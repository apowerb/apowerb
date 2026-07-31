"""Gmail (Google Pub/Sub) webhook notification handler.

Handles incoming push notifications from Google Cloud Pub/Sub,
which are triggered by Gmail mailbox changes via ``users.watch()``.
"""

import base64
import json
import string
import time
from datetime import datetime, timezone
from logging import getLogger

from fastapi import BackgroundTasks, Request, Response
from sqlalchemy import select

from apowerb.configs.settings import get_settings
from apowerb.helpers.database import sessionmanager
from apowerb.helpers.google_oidc import verify_gmail_push_jwt
from apowerb.integrations.gmail_webhook import GmailWebhookService
from apowerb.models import Integration, WebhookLog, WebhookSubscription
from apowerb.schema.webhook_schema import GmailPubSubNotification

from ._common import (
    create_webhook_notification,
    finalise_webhook_log,
    run_agent_for_webhook,
)

logger = getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_header(headers: list[dict], name: str) -> str:
    """Extract a specific header value from a Gmail message headers list."""
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _extract_body(email_data: dict, max_chars: int = 5000) -> str:
    """Extract the plain-text body from a Gmail message (full format).

    Handles both single-part and multipart messages.  Falls back to
    the ``snippet`` field if no plain-text part is found.
    """
    payload = email_data.get("payload", {})

    # Single-part message: body is directly in payload.body.data
    body_data = payload.get("body", {}).get("data", "")
    if body_data:
        try:
            return base64.urlsafe_b64decode(body_data + "==").decode("utf-8", errors="replace")[:max_chars]
        except Exception:
            pass

    # Multipart: look for text/plain in parts
    for part in payload.get("parts", []):
        if part.get("mimeType") == "text/plain":
            part_data = part.get("body", {}).get("data", "")
            if part_data:
                try:
                    return base64.urlsafe_b64decode(part_data + "==").decode("utf-8", errors="replace")[:max_chars]
                except Exception:
                    pass

    # Fallback to snippet
    return email_data.get("snippet", "")


def _build_agent_message(email_data: dict, template: str | None) -> str:
    """Build agent message from Gmail email data.

    Gmail email headers are stored as a list of ``{"name": ..., "value": ...}``
    dicts, unlike Outlook's flat structure. Uses ``string.Template`` with
    ``safe_substitute`` to prevent format-string injection.
    """
    headers = email_data.get("payload", {}).get("headers", [])

    sender = _get_header(headers, "From")
    subject = _get_header(headers, "Subject") or "(no subject)"
    date = _get_header(headers, "Date")
    body = _extract_body(email_data)

    substitutions = dict(
        sender=sender,
        subject=subject,
        date=date,
        body_preview=body,
        received=date,
    )

    if template:
        try:
            return string.Template(template).safe_substitute(substitutions)
        except Exception as exc:
            logger.warning(
                "[GMAIL WEBHOOK] Template formatting failed (%s), using default", exc
            )

    return (
        f"New email received from {sender}\n"
        f"Subject: {subject}\n"
        f"Date: {date}\n\n"
        f"{body}"
    )


def _extract_sender(email_data: dict) -> str:
    """Extract a human-readable sender string from Gmail email data."""
    headers = email_data.get("payload", {}).get("headers", [])
    return _get_header(headers, "From")


# ---------------------------------------------------------------------------
# Background task
# ---------------------------------------------------------------------------


async def _process_gmail_notification(
    sub_db_id: int,
    user_id: int,
    agent_id: int,
    agent_message_template: str | None,
    start_history_id: str,
    new_history_id: str,
    label_filter: str | None,
) -> None:
    """Background task: fetch Gmail history + trigger agent for each new email.

    Receives scalar values (not ORM objects) to avoid DetachedInstanceError
    after the request session is closed.

    Runs outside the request/response cycle so the notification endpoint
    can return 200 immediately (Pub/Sub requires fast responses).
    """
    logger.info(
        "[GMAIL WEBHOOK BG] Processing notification: sub_db_id=%s, "
        "startHistoryId=%s, newHistoryId=%s",
        sub_db_id,
        start_history_id,
        new_history_id,
    )

    log_id: int | None = None
    start_time = time.monotonic()

    try:
        async with sessionmanager.session() as db:
            # -- Create a pending log entry BEFORE running the agent ----------
            log_entry = WebhookLog(
                user_id=user_id,
                subscription_id=sub_db_id,
                agent_id=agent_id,
                trigger_event="created",
                status="pending",
            )
            db.add(log_entry)
            await db.commit()
            await db.refresh(log_entry)
            log_id = log_entry.id

            # 1. Get fresh access token
            access_token = await GmailWebhookService.get_access_token_for_user(
                db, user_id
            )

            # 2. Fetch history since last known historyId
            # "ALL" is our internal convention for "all labels" — pass None to skip filtering
            effective_label = None if label_filter == "ALL" else label_filter
            history_records = await GmailWebhookService.fetch_history(
                access_token=access_token,
                start_history_id=start_history_id,
                label_id=effective_label,
            )

            if not history_records:
                logger.info(
                    "[GMAIL WEBHOOK BG] No new messages in history for sub_db_id=%s",
                    sub_db_id,
                )
                duration_ms = int((time.monotonic() - start_time) * 1000)
                await finalise_webhook_log(
                    log_id,
                    status="success",
                    agent_response="(no new messages in history)",
                    duration_ms=duration_ms,
                )
                # Still update the history cursor
                sub_row = await db.get(WebhookSubscription, sub_db_id)
                if sub_row:
                    sub_row.last_history_id = new_history_id  # type: ignore[assignment]
                    sub_row.last_notification_at = datetime.now(timezone.utc)  # type: ignore[assignment]
                    await db.commit()
                return

            # 3. Collect all new message IDs from history
            message_ids: list[str] = []
            seen: set[str] = set()
            for record in history_records:
                for added in record.get("messagesAdded", []):
                    msg = added.get("message", {})
                    msg_id = msg.get("id")
                    if msg_id and msg_id not in seen:
                        seen.add(msg_id)
                        message_ids.append(msg_id)

            if not message_ids:
                logger.info(
                    "[GMAIL WEBHOOK BG] History records found but no messagesAdded for sub_db_id=%s",
                    sub_db_id,
                )
                duration_ms = int((time.monotonic() - start_time) * 1000)
                await finalise_webhook_log(
                    log_id,
                    status="success",
                    agent_response="(history records found but no new messages)",
                    duration_ms=duration_ms,
                )
                sub_row = await db.get(WebhookSubscription, sub_db_id)
                if sub_row:
                    sub_row.last_history_id = new_history_id  # type: ignore[assignment]
                    sub_row.last_notification_at = datetime.now(timezone.utc)  # type: ignore[assignment]
                    await db.commit()
                return

            logger.info(
                "[GMAIL WEBHOOK BG] Found %d new message(s) for sub_db_id=%s",
                len(message_ids),
                sub_db_id,
            )

            # 4. Process each new message
            last_email_subject = ""
            last_sender_str = ""
            # Single conversation per webhook subscription
            session_id = f"webhook_{sub_db_id}"
            agent_response_text = ""

            for msg_id in message_ids:
                try:
                    # Fetch email details
                    email_data = await GmailWebhookService.fetch_email(
                        access_token, msg_id
                    )

                    # Build agent message
                    message_text = _build_agent_message(
                        email_data, agent_message_template
                    )
                    sender_str = _extract_sender(email_data)
                    headers = email_data.get("payload", {}).get("headers", [])
                    email_subject = _get_header(headers, "Subject") or "(no subject)"

                    last_email_subject = email_subject
                    last_sender_str = sender_str

                    # Update log with email metadata
                    log_row = await db.get(WebhookLog, log_id)
                    if log_row:
                        log_row.agent_message = message_text  # type: ignore[assignment]
                        log_row.email_subject = email_subject[:500] if email_subject else None  # type: ignore[assignment]
                        log_row.email_sender = sender_str[:500] if sender_str else None  # type: ignore[assignment]
                        await db.commit()

                    # Run agent – reuse the single session for all messages
                    agent_response_text = await run_agent_for_webhook(
                        user_id=user_id,
                        agent_id=agent_id,
                        sub_db_id=sub_db_id,
                        session_id=session_id,
                        message_text=message_text,
                    )

                    logger.info(
                        "[GMAIL WEBHOOK BG] Agent agent%s processed message %s for sub_db_id=%s",
                        agent_id,
                        msg_id,
                        sub_db_id,
                    )

                except Exception as msg_exc:
                    logger.error(
                        "[GMAIL WEBHOOK BG] Error processing message %s: %s",
                        msg_id,
                        msg_exc,
                        exc_info=True,
                    )
                    # Continue with the next message
                    continue

            duration_ms = int((time.monotonic() - start_time) * 1000)

            # 5. Update log entry with success
            await finalise_webhook_log(
                log_id,
                status="success",
                agent_response=agent_response_text,
                duration_ms=duration_ms,
            )

            # 6. Update last_history_id and last_notification_at
            sub_row = await db.get(WebhookSubscription, sub_db_id)
            if sub_row:
                sub_row.last_history_id = new_history_id  # type: ignore[assignment]
                sub_row.last_notification_at = datetime.now(timezone.utc)  # type: ignore[assignment]
                await db.commit()

            # 7. Create user notification + push SSE
            await create_webhook_notification(
                user_id,
                title="New email processed",
                message=(
                    f"Agent processed {len(message_ids)} Gmail email(s). "
                    f"Latest: \"{last_email_subject}\" from {last_sender_str}"
                ),
                link=f"/chat?agent=agent{agent_id}&session={session_id}",
                metadata={
                    "agent_id": agent_id,
                    "email_subject": last_email_subject,
                    "email_sender": last_sender_str,
                    "webhook_log_id": log_id,
                    "message_count": len(message_ids),
                    "provider": "google_gmail",
                },
            )

    except Exception as exc:
        logger.error(
            "[GMAIL WEBHOOK BG] Error processing notification for sub_db_id=%s: %s",
            sub_db_id,
            exc,
            exc_info=True,
        )
        duration_ms = int((time.monotonic() - start_time) * 1000)
        if log_id is not None:
            await finalise_webhook_log(
                log_id,
                status="error",
                error_message=str(exc),
                duration_ms=duration_ms,
            )
        await create_webhook_notification(
            user_id,
            title="Gmail webhook error",
            message=f"Agent failed to process Gmail email: {str(exc)[:200]}",
            link="/webhooks",
            metadata={},
            notif_type="error",
        )


# ---------------------------------------------------------------------------
# Public handler called by the dispatcher
# ---------------------------------------------------------------------------


async def handle_gmail_notification(
    request: Request,
    background_tasks: BackgroundTasks,
    **kwargs,
) -> Response:
    """Google Pub/Sub push notification receiver.

    **This endpoint is publicly accessible** -- it is NOT behind auth
    middleware because Google Pub/Sub calls it directly.

    Pub/Sub push payload::

        {
            "message": {
                "data": "<base64 of {emailAddress, historyId}>",
                "messageId": "...",
                "publishTime": "..."
            },
            "subscription": "projects/.../subscriptions/..."
        }

    The ``data`` field is a base64-encoded JSON string containing
    ``emailAddress`` and ``historyId``.  We decode it, look up matching
    Gmail subscriptions for that email address, and spawn background
    tasks to fetch new messages and trigger agents.

    Always returns HTTP 200 to prevent Pub/Sub retries.

    Before any processing, the Google-signed OIDC JWT provided in the
    ``Authorization: Bearer`` header is verified against the configured
    audience. Requests without a valid token are rejected with 401/403
    (which Pub/Sub will treat as a delivery failure and retry — the
    correct behaviour when authentication actually fails).
    """
    settings = get_settings()

    if settings.webhook_dev_skip_sig:
        logger.warning(
            "[GMAIL WEBHOOK] Skipping OIDC signature verification "
            "(WEBHOOK_DEV_SKIP_SIG=true; dev-only)."
        )
    else:
        # Let HTTPException bubble up so FastAPI returns 401 / 403.
        verify_gmail_push_jwt(
            request.headers.get("Authorization"),
            audience=settings.google_webhook_audience,
        )

    try:
        raw_body = await request.json()
        payload = GmailPubSubNotification(**raw_body)
    except Exception as exc:
        logger.error("[GMAIL WEBHOOK] Failed to parse payload: %s", exc)
        return Response(status_code=200)  # Must return 200 to avoid Pub/Sub retries

    # Decode the base64 data
    try:
        decoded = base64.b64decode(payload.message.data).decode("utf-8")
        notification_data = json.loads(decoded)
    except Exception as exc:
        logger.error("[GMAIL WEBHOOK] Failed to decode message data: %s", exc)
        return Response(status_code=200)

    email_address = notification_data.get("emailAddress")
    history_id = str(notification_data.get("historyId", ""))

    if not email_address or not history_id:
        logger.warning("[GMAIL WEBHOOK] Missing emailAddress or historyId")
        return Response(status_code=200)

    logger.info(
        "[GMAIL WEBHOOK] Notification for %s, historyId=%s",
        email_address,
        history_id,
    )

    # Look up active Gmail subscriptions for this email address
    async with sessionmanager.session() as db:
        # Find the integration by email to get user_id
        result = await db.execute(
            select(Integration).where(
                Integration.provider == "google_gmail",
                Integration.provider_username == email_address,
            )
        )
        integration = result.scalar_one_or_none()

        if not integration:
            logger.warning(
                "[GMAIL WEBHOOK] No Gmail integration for %s", email_address
            )
            return Response(status_code=200)

        # Find active Gmail subscriptions for this user
        result = await db.execute(
            select(WebhookSubscription).where(
                WebhookSubscription.user_id == integration.user_id,
                WebhookSubscription.provider == "google_gmail",
                WebhookSubscription.status == "active",
            )
        )
        subscriptions = result.scalars().all()

        if not subscriptions:
            logger.warning(
                "[GMAIL WEBHOOK] No active Gmail subscriptions for user_id=%s",
                integration.user_id,
            )
            return Response(status_code=200)

        for sub in subscriptions:
            # Skip if historyId is not newer (compare numerically)
            if sub.last_history_id:
                try:
                    if int(history_id) <= int(sub.last_history_id):
                        logger.debug(
                            "[GMAIL WEBHOOK] Skipping stale historyId for sub_db_id=%s "
                            "(current=%s, last=%s)",
                            sub.id,
                            history_id,
                            sub.last_history_id,
                        )
                        continue
                except (ValueError, TypeError):
                    # If comparison fails, process anyway
                    pass

            background_tasks.add_task(
                _process_gmail_notification,
                sub.id,
                sub.user_id,
                sub.agent_id,
                sub.agent_message_template,
                sub.last_history_id or history_id,
                history_id,
                sub.resource,  # label filter like "INBOX"
            )

    return Response(status_code=200)  # Pub/Sub expects 200

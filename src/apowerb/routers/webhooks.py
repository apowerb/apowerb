"""Webhook router -- subscription CRUD + unified notification dispatch.

Notification handling is delegated to service-specific handlers in
``routers/webhook_handlers/`` (one module per service: Outlook, Gmail, …).
This file stays lean: only CRUD + the ``/{service}/notifications`` dispatcher.

Endpoints:
    POST   /api/webhooks/subscriptions                          (auth)  Create subscription
    GET    /api/webhooks/subscriptions                          (auth)  List subscriptions
    DELETE /api/webhooks/subscriptions/{subscription_db_id}     (auth)  Delete subscription
    POST   /api/webhooks/subscriptions/{subscription_db_id}/renew (auth) Renew subscription
    POST   /api/webhooks/{service}/notifications                (public) Provider-specific
    GET    /api/webhooks/logs                                   (auth)  Webhook execution logs
    GET    /api/webhooks/logs/{log_id}                          (auth)  Single webhook log by id
    POST   /api/webhooks/logs/{log_id}/retrigger                (auth)  Re-trigger a log from its stored payload
"""

import uuid
from datetime import datetime, timedelta, timezone
from logging import getLogger

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse
from pathlib import Path as _Path

from apowerb.storage.filename import sanitize_filename
from apowerb.storage.webhook_attachments import resolve_attachment_path
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from apowerb.auth.dependencies import get_current_user
from apowerb.configs.settings import get_settings
from apowerb.helpers.database import get_db, sessionmanager
from apowerb.helpers.emails import get_domain_from_email
from apowerb.integrations.gmail_webhook import GmailWebhookService
from apowerb.integrations.outlook_webhook import OutlookWebhookService
from apowerb.models import Integration, User, WebhookLog, WebhookSubscription
from apowerb.routers.webhook_handlers import NOTIFICATION_HANDLERS
from apowerb.schema.webhook_schema import (
    WebhookSubscriptionCreate,
    WebhookSubscriptionList,
    WebhookSubscriptionResponse,
    WebhookSubscriptionUpdate,
)
from apowerb.users import schemas as user_schemas

logger = getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _subscription_to_response(sub: WebhookSubscription) -> WebhookSubscriptionResponse:
    """Convert an ORM WebhookSubscription to the API response schema."""
    return WebhookSubscriptionResponse(
        id=sub.id,
        provider=sub.provider,
        subscription_id=sub.subscription_id,
        resource=sub.resource,
        change_type=sub.change_type,
        agent_id=sub.agent_id,
        agent_message_template=sub.agent_message_template,
        status=sub.status,
        expiration_datetime=(
            sub.expiration_datetime.isoformat() if sub.expiration_datetime else None
        ),
        created_at=(
            sub.created_at.isoformat() if sub.created_at else None
        ),
    )


# ---------------------------------------------------------------------------
# 1. POST /api/webhooks/subscriptions -- Create subscription (authenticated)
# ---------------------------------------------------------------------------


@router.post("/subscriptions", status_code=status.HTTP_201_CREATED)
async def create_subscription(
    body: WebhookSubscriptionCreate,
    db: AsyncSession = Depends(get_db),
    current_user: user_schemas.User = Depends(get_current_user),
) -> WebhookSubscriptionResponse:
    """Create a new webhook subscription.

    Supports both Microsoft Outlook (via Graph API) and Google Gmail
    (via Pub/Sub watch).  The ``provider`` field in the request body
    determines which path is taken.

    1. Verifies the user has an active integration for the provider.
    2. Obtains a fresh access token via the stored refresh token.
    3. Registers the push subscription with the provider.
    4. Persists a ``WebhookSubscription`` row in the database.
    """
    settings = get_settings()
    user_id = current_user.user_id
    logger.info(
        "[WEBHOOK] Creating subscription: user_id=%s, provider=%s, agent_id=%s, resource=%s",
        user_id,
        body.provider,
        body.agent_id,
        body.resource,
    )

    # ── Gmail path ────────────────────────────────────────────────────
    if body.provider == "google_gmail":
        return await _create_gmail_subscription(body, db, user_id, settings)

    # ── Outlook path (default / existing) ─────────────────────────────

    # 1. Verify the user has an active Outlook integration
    result = await db.execute(
        select(Integration).where(
            Integration.user_id == user_id,
            Integration.provider == "microsoft_outlook",
        )
    )
    integration = result.scalar_one_or_none()
    if not integration:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No active Outlook integration found. "
            "Please connect your Outlook account first via the Integrations page.",
        )
    # Capture scalar before any commit expires the ORM object
    integration_db_id = integration.id

    # 2. Get fresh access token
    try:
        access_token = await OutlookWebhookService.get_access_token_for_user(
            db, user_id
        )
    except RuntimeError as exc:
        logger.error("[WEBHOOK] Token refresh failed for user_id=%s: %s", user_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        )

    # 3. Generate client_state and notification URL
    client_state = OutlookWebhookService.generate_client_state()
    notification_url = f"{settings.public_base_url}/api/webhooks/outlook/notifications"

    # 4. Register subscription with Microsoft Graph
    try:
        graph_sub = await OutlookWebhookService.create_subscription(
            access_token=access_token,
            notification_url=notification_url,
            resource=body.resource,
            change_type=body.change_type,
            client_state=client_state,
        )
    except RuntimeError as exc:
        logger.error("[WEBHOOK] Graph subscription creation failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to create Microsoft Graph subscription: {exc}",
        )

    # 5. Parse expiration from Graph response
    expiration_str = graph_sub.get("expirationDateTime")
    expiration_dt = None
    if expiration_str:
        try:
            expiration_dt = datetime.fromisoformat(
                expiration_str.replace("Z", "+00:00")
            )
        except (ValueError, TypeError):
            logger.warning(
                "[WEBHOOK] Could not parse expirationDateTime: %s", expiration_str
            )

    # 6. Persist in database
    subscription = WebhookSubscription(
        user_id=user_id,
        integration_id=integration_db_id,
        provider=body.provider,
        subscription_id=graph_sub.get("id"),
        resource=body.resource,
        change_type=body.change_type,
        notification_url=notification_url,
        client_state=client_state,
        expiration_datetime=expiration_dt,
        agent_id=body.agent_id,
        agent_message_template=body.agent_message_template,
        status="active",
    )
    db.add(subscription)
    await db.commit()

    # Re-fetch to get server-generated fields (id, created_at)
    result = await db.execute(
        select(WebhookSubscription).where(
            WebhookSubscription.subscription_id == graph_sub.get("id"),
        )
    )
    saved = result.scalar_one()
    response = _subscription_to_response(saved)

    logger.info(
        "[WEBHOOK] Subscription created: db_id=%s, graph_id=%s, agent_id=%s",
        response.id,
        response.subscription_id,
        response.agent_id,
    )

    return response


async def _create_gmail_subscription(
    body: WebhookSubscriptionCreate,
    db: AsyncSession,
    user_id: int,
    settings,
) -> WebhookSubscriptionResponse:
    """Handle Gmail-specific subscription creation via Pub/Sub watch.

    Separated from the main ``create_subscription`` endpoint for clarity.
    """
    # 1. Verify the user has an active Gmail integration
    result = await db.execute(
        select(Integration).where(
            Integration.user_id == user_id,
            Integration.provider == "google_gmail",
        )
    )
    integration = result.scalar_one_or_none()
    if not integration:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No active Gmail integration found. "
            "Please connect your Google account first via the Integrations page.",
        )
    integration_db_id = integration.id

    # 2. Get fresh access token
    try:
        access_token = await GmailWebhookService.get_access_token_for_user(
            db, user_id
        )
    except (RuntimeError, httpx.HTTPError) as exc:
        logger.error("[GMAIL WEBHOOK] Token refresh failed for user_id=%s: %s", user_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        )

    # 3. Build Pub/Sub topic name and determine label_ids
    if not settings.gmail_pubsub_project_id or not settings.gmail_pubsub_topic:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Gmail Pub/Sub is not configured on this server "
            "(gmail_pubsub_project_id / gmail_pubsub_topic missing).",
        )

    topic_name = (
        f"projects/{settings.gmail_pubsub_project_id}"
        f"/topics/{settings.gmail_pubsub_topic}"
    )

    # resource for Gmail = label name (e.g. "INBOX", "SENT")
    # "ALL" means watch all labels → pass empty list to Gmail API
    label_ids = [] if body.resource == "ALL" else ([body.resource] if body.resource else ["INBOX"])

    # 4. Register watch with Gmail API
    try:
        watch_result = await GmailWebhookService.watch_mailbox(
            access_token=access_token,
            topic_name=topic_name,
            label_ids=label_ids,
        )
    except (RuntimeError, httpx.HTTPError) as exc:
        logger.error("[GMAIL WEBHOOK] Watch creation failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to create Gmail watch: {exc}",
        )

    # 5. Parse expiration from watch response (epoch milliseconds)
    expiration_dt = None
    exp_ms = watch_result.get("expiration")
    if exp_ms:
        try:
            expiration_dt = datetime.fromtimestamp(
                int(exp_ms) / 1000, tz=timezone.utc
            )
        except (ValueError, TypeError, OSError):
            logger.warning(
                "[GMAIL WEBHOOK] Could not parse expiration: %s", exp_ms
            )

    # 6. Generate a unique subscription_id (Gmail watch doesn't return one)
    gmail_subscription_id = f"gmail-watch-{uuid.uuid4().hex[:16]}"
    client_state = GmailWebhookService.generate_client_state()
    notification_url = f"{settings.public_base_url}/api/webhooks/gmail/notifications"

    history_id = str(watch_result.get("historyId", ""))

    # 7. Persist in database
    subscription = WebhookSubscription(
        user_id=user_id,
        integration_id=integration_db_id,
        provider="google_gmail",
        subscription_id=gmail_subscription_id,
        resource=body.resource or "INBOX",
        change_type=body.change_type,
        notification_url=notification_url,
        client_state=client_state,
        expiration_datetime=expiration_dt,
        agent_id=body.agent_id,
        agent_message_template=body.agent_message_template,
        last_history_id=history_id,
        status="active",
    )
    db.add(subscription)
    await db.commit()

    # Re-fetch to get server-generated fields (id, created_at)
    result = await db.execute(
        select(WebhookSubscription).where(
            WebhookSubscription.subscription_id == gmail_subscription_id,
        )
    )
    saved = result.scalar_one()
    response = _subscription_to_response(saved)

    logger.info(
        "[GMAIL WEBHOOK] Subscription created: db_id=%s, historyId=%s, agent_id=%s",
        response.id,
        history_id,
        response.agent_id,
    )

    return response


# ---------------------------------------------------------------------------
# 2. GET /api/webhooks/subscriptions -- List subscriptions (authenticated)
# ---------------------------------------------------------------------------


@router.get("/subscriptions")
async def list_subscriptions(
    db: AsyncSession = Depends(get_db),
    current_user: user_schemas.User = Depends(get_current_user),
) -> WebhookSubscriptionList:
    """Return all webhook subscriptions owned by the current user."""
    result = await db.execute(
        select(WebhookSubscription).where(
            WebhookSubscription.user_id == current_user.user_id,
        )
    )
    subscriptions = result.scalars().all()

    return WebhookSubscriptionList(
        subscriptions=[_subscription_to_response(s) for s in subscriptions]
    )


# ---------------------------------------------------------------------------
# 3. DELETE /api/webhooks/subscriptions/{id} -- Delete subscription (auth)
# ---------------------------------------------------------------------------


@router.delete("/subscriptions/{subscription_db_id}", status_code=status.HTTP_200_OK)
async def delete_subscription(
    subscription_db_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Delete a webhook subscription from both the provider and the database."""
    # Look up by id WITHOUT owner filter, then check ownership explicitly.
    # This lets us return 404 vs 403 distinctly (instead of masking cross-owner
    # access as 404, which hides authorization failures from observability).
    result = await db.execute(
        select(WebhookSubscription).where(
            WebhookSubscription.id == subscription_db_id,
        )
    )
    subscription = result.scalar_one_or_none()

    if not subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Subscription {subscription_db_id} not found.",
        )

    if subscription.user_id != current_user.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this subscription.",
        )

    # Try to delete from the provider (best effort)
    if subscription.provider == "google_gmail":
        try:
            access_token = await GmailWebhookService.get_access_token_for_user(
                db, current_user.user_id
            )
            await GmailWebhookService.stop_watch(access_token)
        except Exception as exc:
            logger.warning(
                "[GMAIL WEBHOOK] Could not stop Gmail watch for sub_db_id=%s: %s",
                subscription_db_id,
                exc,
            )
    elif subscription.subscription_id:
        # Outlook / Microsoft Graph
        try:
            access_token = await OutlookWebhookService.get_access_token_for_user(
                db, current_user.user_id
            )
            await OutlookWebhookService.delete_subscription(
                access_token, subscription.subscription_id
            )
        except Exception as exc:
            # Log but don't block DB deletion -- the subscription may have
            # already expired on Microsoft's side.
            logger.warning(
                "[WEBHOOK] Could not delete Graph subscription %s: %s",
                subscription.subscription_id,
                exc,
            )

    # Capture identifying fields before commit() expires the ORM instance
    # (post-commit lazy-load under asyncpg raises MissingGreenlet).
    sub_provider = subscription.provider
    sub_external_id = subscription.subscription_id

    # Delete from database
    await db.delete(subscription)
    await db.commit()

    logger.info(
        "[WEBHOOK] Subscription deleted: db_id=%s, provider=%s, subscription_id=%s",
        subscription_db_id,
        sub_provider,
        sub_external_id,
    )

    return {"success": True, "message": f"Subscription {subscription_db_id} deleted."}


# ---------------------------------------------------------------------------
# 3b. PATCH /api/webhooks/subscriptions/{id} -- Update subscription (auth)
# ---------------------------------------------------------------------------


@router.patch("/subscriptions/{subscription_db_id}")
async def update_subscription(
    subscription_db_id: int,
    body: WebhookSubscriptionUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: user_schemas.User = Depends(get_current_user),
) -> WebhookSubscriptionResponse:
    """Update a webhook subscription (agent, template, change_type)."""
    result = await db.execute(
        select(WebhookSubscription).where(
            WebhookSubscription.id == subscription_db_id,
            WebhookSubscription.user_id == current_user.user_id,
        )
    )
    subscription = result.scalar_one_or_none()

    if not subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Subscription {subscription_db_id} not found.",
        )

    if body.agent_id is not None:
        subscription.agent_id = body.agent_id  # type: ignore[assignment]
    if body.agent_message_template is not None:
        subscription.agent_message_template = body.agent_message_template  # type: ignore[assignment]
    if body.change_type is not None:
        subscription.change_type = body.change_type  # type: ignore[assignment]

    subscription.updated_at = datetime.now(timezone.utc)  # type: ignore[assignment]
    await db.commit()

    refreshed = await db.execute(
        select(WebhookSubscription).where(WebhookSubscription.id == subscription_db_id)
    )
    saved = refreshed.scalar_one()

    logger.info(
        "[WEBHOOK] Subscription updated: db_id=%s, agent_id=%s",
        subscription_db_id,
        saved.agent_id,
    )

    return _subscription_to_response(saved)


# ---------------------------------------------------------------------------
# 4. POST /api/webhooks/subscriptions/{id}/renew -- Renew subscription (auth)
# ---------------------------------------------------------------------------


@router.post("/subscriptions/{subscription_db_id}/renew")
async def renew_subscription(
    subscription_db_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: user_schemas.User = Depends(get_current_user),
) -> WebhookSubscriptionResponse:
    """Renew (extend) a webhook subscription on the provider."""
    # Look up and verify ownership in one query
    result = await db.execute(
        select(WebhookSubscription).where(
            WebhookSubscription.id == subscription_db_id,
            WebhookSubscription.user_id == current_user.user_id,
        )
    )
    subscription = result.scalar_one_or_none()

    if not subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Subscription {subscription_db_id} not found.",
        )

    # ── Gmail renewal ─────────────────────────────────────────────────
    if subscription.provider == "google_gmail":
        return await _renew_gmail_subscription(subscription, db, current_user.user_id)

    # ── Outlook renewal (default) ─────────────────────────────────────
    graph_sub_id = subscription.subscription_id
    if not graph_sub_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This subscription has no Microsoft Graph ID (never fully registered).",
        )

    # Get fresh access token
    # NOTE: this may commit (token rotation), which expires all ORM objects
    # in the session.  We saved graph_sub_id above to avoid a lazy-load on
    # the now-expired `subscription` instance (MissingGreenlet in async).
    try:
        access_token = await OutlookWebhookService.get_access_token_for_user(
            db, current_user.user_id
        )
    except (RuntimeError, httpx.HTTPError) as exc:
        logger.error("[WEBHOOK] Token refresh failed for renew: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        )

    # Renew on Microsoft Graph
    try:
        renewed = await OutlookWebhookService.renew_subscription(
            access_token, graph_sub_id
        )
    except LookupError:
        # 404 — subscription no longer exists on Microsoft Graph; clean up.
        logger.warning(
            "[WEBHOOK] Subscription db_id=%s (graph_id=%s) not found on "
            "Microsoft Graph — deleting local record.",
            subscription_db_id,
            graph_sub_id,
        )
        await db.execute(
            delete(WebhookSubscription).where(
                WebhookSubscription.id == subscription_db_id
            )
        )
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=(
                "The webhook subscription no longer exists on Microsoft Graph "
                "and has been deleted. Please create a new one."
            ),
        )
    except (RuntimeError, httpx.HTTPError) as exc:
        logger.error("[WEBHOOK] Renewal failed: %s", exc)
        # Mark as error in DB
        subscription.status = "error"  # type: ignore[assignment]
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to renew subscription on Microsoft Graph: {exc}",
        )

    # Update expiration in DB
    new_expiry_str = renewed.get("expirationDateTime")
    if new_expiry_str:
        try:
            subscription.expiration_datetime = datetime.fromisoformat(  # type: ignore[assignment]
                new_expiry_str.replace("Z", "+00:00")
            )
        except (ValueError, TypeError):
            pass

    subscription.status = "active"  # type: ignore[assignment]
    await db.commit()

    # Re-fetch to get updated fields cleanly
    result = await db.execute(
        select(WebhookSubscription).where(
            WebhookSubscription.id == subscription_db_id,
        )
    )
    refreshed = result.scalar_one()
    response = _subscription_to_response(refreshed)

    logger.info(
        "[WEBHOOK] Subscription renewed: db_id=%s, new_expiry=%s",
        subscription_db_id,
        response.expiration_datetime,
    )

    return response


async def _renew_gmail_subscription(
    subscription: WebhookSubscription,
    db: AsyncSession,
    user_id: int,
) -> WebhookSubscriptionResponse:
    """Renew a Gmail watch subscription by re-calling ``watch_mailbox()``.

    Gmail watches expire after 7 days.  Renewal is done by re-calling
    the watch endpoint (there is no separate "renew" API).
    """
    settings = get_settings()

    # Save attributes before token refresh — get_access_token_for_user may
    # commit (token rotation), which expires all ORM objects in the session.
    sub_resource = subscription.resource

    try:
        access_token = await GmailWebhookService.get_access_token_for_user(
            db, user_id
        )
    except (RuntimeError, httpx.HTTPError) as exc:
        logger.error("[GMAIL WEBHOOK] Token refresh failed for renew: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        )

    topic_name = (
        f"projects/{settings.gmail_pubsub_project_id}"
        f"/topics/{settings.gmail_pubsub_topic}"
    )
    label_ids = [] if sub_resource == "ALL" else ([sub_resource] if sub_resource else ["INBOX"])

    try:
        watch_result = await GmailWebhookService.watch_mailbox(
            access_token=access_token,
            topic_name=topic_name,
            label_ids=label_ids,
        )
    except (RuntimeError, httpx.HTTPError) as exc:
        logger.error("[GMAIL WEBHOOK] Renewal failed: %s", exc)
        subscription.status = "error"  # type: ignore[assignment]
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to renew Gmail watch: {exc}",
        )

    # Update expiration and historyId
    exp_ms = watch_result.get("expiration")
    if exp_ms:
        try:
            subscription.expiration_datetime = datetime.fromtimestamp(  # type: ignore[assignment]
                int(exp_ms) / 1000, tz=timezone.utc
            )
        except (ValueError, TypeError, OSError):
            pass

    new_history_id = watch_result.get("historyId")
    if new_history_id:
        subscription.last_history_id = str(new_history_id)  # type: ignore[assignment]

    subscription.status = "active"  # type: ignore[assignment]
    await db.commit()

    # Re-fetch to get updated fields cleanly
    result = await db.execute(
        select(WebhookSubscription).where(
            WebhookSubscription.id == subscription.id,
        )
    )
    refreshed = result.scalar_one()
    response = _subscription_to_response(refreshed)

    logger.info(
        "[GMAIL WEBHOOK] Subscription renewed: db_id=%s, new_expiry=%s",
        subscription.id,
        response.expiration_datetime,
    )

    return response



# ---------------------------------------------------------------------------
# 5. POST /api/webhooks/{service}/notifications -- Unified dispatch
# ---------------------------------------------------------------------------


@router.post("/{service}/notifications")
async def receive_notification(
    service: str,
    request: Request,
    background_tasks: BackgroundTasks,
    validationToken: str | None = Query(default=None),
):
    """Unified webhook notification receiver.

    **This endpoint is publicly accessible** -- providers (Microsoft Graph,
    Google Pub/Sub, etc.) call it directly.

    The ``{service}`` path segment selects the handler:
        - ``outlook``  -- Microsoft Graph push notifications
        - ``gmail``    -- Google Cloud Pub/Sub push notifications
        - (extensible: add new handlers in ``webhook_handlers/``)

    Unknown services receive a ``200 OK`` (to prevent provider retries).
    """
    handler = NOTIFICATION_HANDLERS.get(service)
    if not handler:
        logger.warning("[WEBHOOK] Unknown notification service: %s", service)
        return Response(status_code=200)

    return await handler(
        request,
        background_tasks,
        validationToken=validationToken,
    )


# ---------------------------------------------------------------------------
# 6. GET /api/webhooks/logs -- Webhook execution logs (authenticated)
# ---------------------------------------------------------------------------


# Statuses a caller may filter on. Anything else is rejected rather than
# silently returning everything -- a filter that quietly does nothing is worse
# than one that errors, because the operator trusts the empty-looking result.
_LOG_STATUSES = frozenset(
    {"pending", "in_progress", "success", "error", "retrying"}
)

# "failed" is what an operator looks for, and it spans two stored states.
_STATUS_GROUPS = {"failed": ("error", "retrying")}


def _parse_log_moment(value: str, field: str) -> datetime:
    """Parse an ISO date or datetime, and make it timezone-aware.

    A bare date means the whole day in UTC: ``since=2026-08-21`` starts at
    00:00 and ``until=2026-08-21`` must still include 23:59, so the caller
    does not have to know that comparing a date against a timestamp silently
    excludes the day itself.
    """
    raw = (value or "").strip()
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"{field} must be an ISO date or datetime, got {value!r}",
        )
    if len(raw) == 10 and field == "until":
        moment = moment.replace(hour=23, minute=59, second=59, microsecond=999999)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


@router.get("/logs")
async def list_webhook_logs(
    db: AsyncSession = Depends(get_db),
    current_user: user_schemas.User = Depends(get_current_user),
    subscription_id: int | None = Query(default=None),
    agent_id: str | None = Query(default=None),
    status: str | None = Query(
        default=None,
        description="One or more statuses, comma separated. "
        "'failed' expands to error+retrying.",
    ),
    since: str | None = Query(default=None, description="ISO date or datetime"),
    until: str | None = Query(default=None, description="ISO date or datetime"),
    q: str | None = Query(default=None, description="Search subject and sender"),
    classification: str | None = Query(
        default=None, description="Agent classification, e.g. 'ar' or 'not_ar'"
    ),
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0),
):
    """Return webhook execution logs for the current user.

    Filters: ``subscription_id``, ``agent_id``, ``status``, ``since`` /
    ``until``, ``q`` (subject or sender), ``classification``. They combine
    with AND. Pagination via ``limit`` / ``offset``, newest-first.

    Note: the ``status`` parameter shadows fastapi's ``status`` module inside
    this function; raise with literal codes here rather than ``status.HTTP_*``.

    Filtering happens here, not in the browser: a client can only filter the
    page it holds, which turns "show me the failures" into "show me the
    failures among the last 40" -- the same answer for a healthy day and a
    catastrophic one.
    """
    filters = [WebhookLog.user_id == current_user.user_id]
    if subscription_id is not None:
        filters.append(WebhookLog.subscription_id == subscription_id)
    if agent_id:
        filters.append(WebhookLog.agent_id == agent_id)

    if status:
        wanted: set[str] = set()
        for token in status.split(","):
            token = token.strip().lower()
            if not token:
                continue
            if token in _STATUS_GROUPS:
                wanted.update(_STATUS_GROUPS[token])
            elif token in _LOG_STATUSES:
                wanted.add(token)
            else:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"unknown status {token!r}; expected one of "
                        + ", ".join(sorted(_LOG_STATUSES | set(_STATUS_GROUPS)))
                    ),
                )
        if wanted:
            filters.append(WebhookLog.status.in_(sorted(wanted)))

    if since:
        filters.append(WebhookLog.created_at >= _parse_log_moment(since, "since"))
    if until:
        filters.append(WebhookLog.created_at <= _parse_log_moment(until, "until"))

    if q:
        # Escape the LIKE wildcards: a subject containing '%' must match
        # itself, not everything.
        needle = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        if needle:
            pattern = f"%{needle}%"
            filters.append(
                or_(
                    WebhookLog.email_subject.ilike(pattern, escape="\\"),
                    WebhookLog.email_sender.ilike(pattern, escape="\\"),
                )
            )

    if classification:
        token = classification.strip()
        if token:
            # The classification lives inside the agent's JSON answer. Match
            # the quoted value so 'ar' cannot also match 'not_ar'.
            filters.append(
                WebhookLog.agent_response.ilike(f'%"email_classification": "{token}"%')
            )

    total = await db.scalar(
        select(func.count()).select_from(WebhookLog).where(*filters)
    )

    query = (
        select(WebhookLog)
        .where(*filters)
        .order_by(WebhookLog.created_at.desc())
        .limit(limit)
        .offset(offset)
    )

    result = await db.execute(query)
    logs = result.scalars().all()

    return {
        "total": total or 0,
        "logs": [
            {
                "id": log.id,
                "subscription_id": log.subscription_id,
                "agent_id": log.agent_id,
                "trigger_event": log.trigger_event,
                "email_subject": log.email_subject,
                "email_sender": log.email_sender,
                "agent_message": log.agent_message,
                "agent_response": log.agent_response,
                "status": log.status,
                "error_message": log.error_message,
                "duration_ms": log.duration_ms,
                "created_at": log.created_at.isoformat() if log.created_at else None,
                # Exposed so the dashboard can show "PJ (N)" badges on each
                # row without an extra round-trip per log. Body HTML is
                # intentionally NOT included (50 logs × ~5kB = 250kB/page).
                "attachments": log.attachments or [],
            }
            for log in logs
        ]
    }


# ---------------------------------------------------------------------------
# 7. GET /api/webhooks/logs/{log_id} -- Single webhook log by id
# ---------------------------------------------------------------------------


async def _load_log_for_read(db: AsyncSession, log_id: int, current_user) -> WebhookLog | None:
    """Load a webhook log the caller is allowed to READ (detail + attachments).

    Read access is granted to the log's owner OR to any user in the same
    organization -- i.e. sharing the owner's email domain, the same rule
    ORGANIZATION-visibility BI dashboards already use. Returns ``None`` when
    the log does not exist or is outside the caller's org, so callers raise a
    uniform 404 (no enumeration oracle). Action endpoints (retry/run) keep
    their strict owner-only filter -- a viewer must never trigger a webhook.
    The raw email body (/body) also stays owner-only: only PJ metadata and
    the attachments themselves are org-shared.
    """
    log = (
        await db.execute(select(WebhookLog).where(WebhookLog.id == log_id))
    ).scalar_one_or_none()
    if log is None:
        return None
    if log.user_id == current_user.user_id:
        return log
    owner = (
        await db.execute(select(User).where(User.user_id == log.user_id))
    ).scalar_one_or_none()
    if owner is None:
        return None
    try:
        owner_domain = get_domain_from_email(owner.email).strip().lower()
        viewer_domain = get_domain_from_email(current_user.email).strip().lower()
    except ValueError:
        return None
    if owner_domain and owner_domain == viewer_domain:
        return log
    return None


@router.get("/logs/{log_id}")
async def get_webhook_log_by_id(
    log_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Return one webhook log by id, scoped to the current user.

    Used by the dashboard deep-link ``/webhooks?log=<id>`` so the
    Activity tab can expand a specific row even when it is not in the
    currently loaded page.
    """
    log = await _load_log_for_read(db, log_id, current_user)
    if log is None:
        raise HTTPException(status_code=404, detail="webhook log not found")

    return {
        "log": {
            "id": log.id,
            "subscription_id": log.subscription_id,
            "agent_id": log.agent_id,
            "trigger_event": log.trigger_event,
            "email_subject": log.email_subject,
            "email_sender": log.email_sender,
            "agent_message": log.agent_message,
            "agent_response": log.agent_response,
            "status": log.status,
            "error_message": log.error_message,
            "duration_ms": log.duration_ms,
            "created_at": log.created_at.isoformat() if log.created_at else None,
            "attachments": log.attachments or [],
        }
    }



# ---------------------------------------------------------------------------
# 8. GET /api/webhooks/logs/{log_id}/body -- Captured email body (HTML)
# ---------------------------------------------------------------------------


@router.get("/logs/{log_id}/body")
async def get_webhook_log_body(
    log_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Return the captured HTML body of a webhook-driven email.

    Returned with ``Content-Type: text/html``. The dashboard renders
    this in a sandboxed iframe (``sandbox=""`` — no scripts, no
    navigation) to defuse XSS from third-party email content.

    404 (not 403) on logs that belong to a different user — no
    enumeration oracle.
    """
    result = await db.execute(
        select(WebhookLog).where(
            WebhookLog.id == log_id,
            WebhookLog.user_id == current_user.user_id,
        )
    )
    log = result.scalar_one_or_none()
    if log is None:
        raise HTTPException(status_code=404, detail="webhook log not found")
    return Response(
        content=log.email_body_html or "",
        media_type="text/html; charset=utf-8",
    )


# ---------------------------------------------------------------------------
# 9. GET /api/webhooks/logs/{log_id}/attachments/{filename}
# ---------------------------------------------------------------------------


@router.get("/logs/{log_id}/attachments/{filename}")
async def get_webhook_log_attachment(
    log_id: int,
    filename: str,
    db: AsyncSession = Depends(get_db),
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Serve one attachment captured at webhook reception time.

    Defence layers, in order:
      1. Ownership check — log must belong to the current user (404
         otherwise; no enumeration oracle).
      2. DB cross-check — the requested filename must appear in the
         ``attachments`` JSONB column of that log row. Prevents serving
         a residual file someone else dropped in the per-log directory.
      3. ``resolve_attachment_path`` — path-traversal-safe lookup that
         confirms the file lives inside ATTACHMENT_ROOT.
      4. ``Content-Type`` taken from the JSONB row (set by the Graph
         metadata at capture time), not re-guessed from the extension.
      5. ``Content-Disposition: inline`` only for PDF and images, so
         the dashboard can iframe-preview them; everything else
         downloads to disk.
    """
    log = await _load_log_for_read(db, log_id, current_user)
    if log is None:
        raise HTTPException(status_code=404, detail="webhook log not found")

    # Defence-in-depth: only files referenced in the row's attachments
    # JSONB are servable, even if the per-log directory happens to
    # contain extra files. The filename comparison runs on the
    # sanitized form (same as what was stored).
    safe = sanitize_filename(filename)
    declared = [a for a in (log.attachments or []) if a.get("filename") == safe]
    if not declared:
        raise HTTPException(status_code=404, detail="attachment not found")
    att = declared[0]

    try:
        path = resolve_attachment_path(log_id, safe)
    except ValueError:
        # File was declared in DB but no longer exists on disk
        # (e.g. cleanup, or pre-PR-188 row). 404 — no 500.
        raise HTTPException(status_code=404, detail="attachment file missing")

    mime = att.get("content_type") or "application/octet-stream"
    disposition = "inline" if (mime.startswith("application/pdf") or mime.startswith("image/")) else "attachment"

    return FileResponse(
        path=str(path),
        media_type=mime,
        filename=safe,
        headers={"Content-Disposition": f'{disposition}; filename="{safe}"'},
    )


# ---------------------------------------------------------------------------
# 10. POST /api/webhooks/logs/{log_id}/retrigger -- Re-queue a log for processing
# ---------------------------------------------------------------------------


@router.post("/logs/{log_id}/retrigger", status_code=status.HTTP_202_ACCEPTED)
async def retrigger_webhook_log(
    log_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Re-queue a webhook log for processing.

    Resets a log row to pending=0 attempts so the backlog worker picks
    it up again.  Atomic UPDATE WHERE status NOT IN
    (\'in_progress\', \'pending\') prevents double-queueing.

    Returns 202 on success.
    409 detail='already_running_or_queued' if the UPDATE touches no row.
    404 if the log does not belong to the current user.
    409 detail='subscription_inactive' if the linked subscription is not active.
    """
    # 1. Load log scoped to current user
    result = await db.execute(
        select(WebhookLog).where(
            WebhookLog.id == log_id,
            WebhookLog.user_id == current_user.user_id,
        )
    )
    log = result.scalar_one_or_none()
    if log is None:
        raise HTTPException(status_code=404, detail="webhook log not found")

    # 2. Load subscription with double-filter (ownership enforced in SQL)
    result_sub = await db.execute(
        select(WebhookSubscription).where(
            WebhookSubscription.id == log.subscription_id,
            WebhookSubscription.user_id == current_user.user_id,
        )
    )
    subscription = result_sub.scalar_one_or_none()
    if subscription is None:
        raise HTTPException(status_code=404, detail="webhook log not found")
    if subscription.status != "active":
        raise HTTPException(status_code=409, detail="subscription_inactive")

    # 3. Atomic UPDATE: only touch non-running rows (5 columns, no payload)
    update_result = await db.execute(
        update(WebhookLog)
        .where(
            WebhookLog.id == log_id,
            WebhookLog.user_id == current_user.user_id,
            WebhookLog.status.not_in(["in_progress", "pending"]),
        )
        .values(
            status="pending",
            attempts=0,
            next_attempt_at=None,
            started_at=None,
            completed_at=None,
            # Deliberate operator re-processing: tell the recorder to
            # REPLACE the existing AR (delete-then-insert) not no-op.
            force_reprocess=True,
        )
        .returning(WebhookLog.id)
    )
    returned_id = update_result.scalar_one_or_none()
    if returned_id is None:
        raise HTTPException(status_code=409, detail="already_running_or_queued")

    await db.commit()
    logger.info(
        "[WEBHOOK] Retrigger log_id=%s by user_id=%s",
        log_id,
        current_user.user_id,
    )
    return {
        "message": "Webhook log re-queued for processing",
        "log_id": log_id,
        "status": "pending",
    }

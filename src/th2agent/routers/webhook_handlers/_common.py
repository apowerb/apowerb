"""Shared helpers for all webhook notification handlers.

Contains utilities that are used by multiple service-specific handlers
(Outlook, Gmail, OneDrive, etc.) to avoid duplication.
"""

import asyncio
import json
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from logging import getLogger

from th2agent.core.adk_runner import (
    create_adk_agent_session,
    delete_adk_agent_session,
    get_adk_session,
    run_adk_agent,
)
from th2agent.core.invocation_context import set_current_invoker
from th2agent.helpers.database import sessionmanager
from th2agent.helpers.notification_bus import notify as push_notification
from th2agent.helpers.security import create_access_token
from th2agent.models import Notification, User, WebhookLog, WebhookSubscription

logger = getLogger(__name__)

# Per-session lock: ensures only one agent run at a time per ADK session.
# This prevents concurrent writes to the same session which can corrupt
# the conversation or cause 504 timeouts on the ADK side.
_session_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


def extract_agent_response(result: dict | list | None) -> str:
    """Extract readable text from ADK agent run result.

    The ADK ``run_adk_agent()`` helper returns different structures
    depending on whether the underlying response was JSON or SSE.
    This function tries the most common paths and falls back to a
    truncated ``str()`` representation.

    When the ADK ``/run`` endpoint is used (non-streaming), the result
    is a ``list[Event]`` -- a list of dicts each containing a
    ``content`` key with ``parts``.  We extract text parts from model
    events and ignore functionCall / functionResponse parts.
    """
    if not result:
        return ""

    # Handle list of ADK events (from /run endpoint)
    if isinstance(result, list):
        texts: list[str] = []
        for event in result:
            if not isinstance(event, dict):
                continue
            content = event.get("content")
            if not content or not isinstance(content, dict):
                continue
            parts = content.get("parts", [])
            for part in parts:
                if (
                    isinstance(part, dict)
                    and part.get("text")
                    and not part.get("functionCall")
                    and not part.get("functionResponse")
                ):
                    texts.append(part["text"])
        return "\n".join(texts) if texts else str(result)[:5000]

    if isinstance(result, dict):
        # Direct content (most common after _collect_sse_response)
        if "content" in result:
            parts = result["content"]
            if isinstance(parts, dict) and "parts" in parts:
                return "\n".join(
                    p.get("text", "") for p in parts["parts"] if p.get("text")
                )
            if isinstance(parts, list):
                return "\n".join(
                    p.get("text", "") for p in parts if isinstance(p, dict) and p.get("text")
                )
            return str(parts)
        # Nested in result
        if "result" in result:
            return extract_agent_response(result["result"])
        # Events list
        if "events" in result and isinstance(result["events"], list):
            event_texts: list[str] = []
            for event in result["events"]:
                actions = event.get("actions", {})
                if "parts" in actions:
                    for part in actions["parts"]:
                        if part.get("text"):
                            event_texts.append(part["text"])
            return "\n".join(event_texts) if event_texts else str(result)[:5000]
    return str(result)[:5000]


async def run_agent_for_webhook(
    *,
    user_id: int,
    agent_id: int,
    sub_db_id: int,
    session_id: str,
    message_text: str,
    initial_state: dict | None = None,
    fresh_session: bool = False,
) -> str:
    """Run an ADK agent with the given message and return the response text.

    Handles JWT creation, session creation (if needed), and agent execution.
    Uses a per-session lock so that concurrent webhook notifications for the
    same subscription are serialised (prevents ADK session corruption).
    Returns the extracted agent response as a string.
    """
    agent_folder = f"agent{agent_id}"

    # Resolve the User row's email and use it as the ADK ``user_id``.
    # The frontend lists/opens sessions via the authenticated user's
    # email (e.g. ``com@scei88.fr``), so the webhook handler MUST store
    # them under the same key — otherwise the operator opens the
    # "Webhook — <agent>" conversation and sees an empty chat because
    # ADK has the events filed under the stringified user_id int
    # instead. Regression observed 2026-05-11: agent6 webhook runs
    # were unreachable from the UI for that exact reason.
    async with sessionmanager.session() as db:
        user_row = await db.get(User, user_id)
        if user_row is None or not user_row.email:
            raise RuntimeError(
                f"Cannot resolve email for user_id={user_id}; ADK "
                "session would be unreachable from the UI"
            )
        user_id_str = user_row.email
        # getattr et non un acces direct : le plafond est best-effort et
        # ne doit jamais casser un webhook par sa propre panne. 'None'
        # resserre sur le quota par defaut, il n ouvre rien.
        owner_plan = getattr(user_row, "plan", None)

    # Bind the webhook agent's owner as the invoker for THIS task so
    # user-personal integrations (Outlook, Gmail, ...) resolve against the
    # owner — not the racy process-global AGENT_OWNER env var. This webhook
    # path reaches ADK's native /run directly, bypassing the /api/adk/run
    # handler where the interactive binding lives; without this, two
    # concurrent webhook agents of different owners could cross mailboxes
    # (same class as incident 2026-07-03). ContextVar is task-local.
    set_current_invoker(user_id_str)

    new_message = {
        "role": "user",
        "parts": [{"text": message_text}],
    }

    # Generate an internal JWT so ADKAuthMiddleware accepts the calls
    # ``sub`` MUST be the user's email: ADKAuthMiddleware binds the invoker
    # from it, so user-personal integrations (Outlook) resolve the webhook
    # agent's owner and not the racy global AGENT_OWNER (incident 2026-07-03).
    internal_token = create_access_token(
        data={"sub": user_id_str, "type": "access"},
        expires_delta=timedelta(minutes=15),
    )

    # Serialise access to the same ADK session
    async with _session_locks[session_id]:
        if fresh_session:
            # FRESH session per run (delete-then-create). UNIQUEMENT quand
            # session_id est UNIQUE par evenement (Outlook: webhook_<log_id>).
            # Reutiliser accumulait l'historique (637 msgs -> boucle > limite
            # 25-calls ADK -> 500) ET sautait initial_state (force_reprocess
            # n'atteignait pas le recorder sur relance). NE PAS activer pour
            # Gmail (session partagee multi-messages). (incident 2026-05-28.)
            try:
                await delete_adk_agent_session(
                    agent_name=agent_folder,
                    user_id=user_id_str,
                    session_id=session_id,
                    token=internal_token,
                )
            except Exception:
                pass  # no prior session -> nothing to delete
            await create_adk_agent_session(
                agent_name=agent_folder,
                user_id=user_id_str,
                session_id=session_id,
                data=initial_state or {},
                token=internal_token,
            )
            logger.info(
                "[WEBHOOK BG] Fresh ADK session %s for agent %s",
                session_id,
                agent_folder,
            )
        else:
            # Session REUTILISEE (get-or-create) — comportement historique pour
            # les webhooks ou la session est partagee entre runs (Gmail:
            # webhook_<sub_db_id> en boucle multi-messages). Preserve
            # l'historique inter-messages.
            try:
                await get_adk_session(
                    agent_name=agent_folder,
                    user_id=user_id_str,
                    session_id=session_id,
                    token=internal_token,
                )
            except Exception:
                await create_adk_agent_session(
                    agent_name=agent_folder,
                    user_id=user_id_str,
                    session_id=session_id,
                    data=initial_state or {},
                    token=internal_token,
                )
                logger.info(
                    "[WEBHOOK BG] Created ADK session %s for agent %s",
                    session_id,
                    agent_folder,
                )

        # Trigger the agent
        # Meme raison que le chemin planifie : ce handler atteint le
        # /run natif d'ADK, les gardes ne sont appliquees nulle part ailleurs.
        from th2agent.core.run_gate import apply_run_guards

        await apply_run_guards(
            agent_name=agent_folder,
            owner_id=user_id_str,
            plan=owner_plan,
        )

        result = await run_adk_agent(
            agent_name=agent_folder,
            user_id=user_id_str,
            session_id=session_id,
            new_message=new_message,
            run_mode="run",
            streaming=False,
            token=internal_token,
        )

    return extract_agent_response(result)


async def finalise_webhook_log(
    log_id: int,
    *,
    status: str,
    agent_response: str = "",
    error_message: str = "",
    duration_ms: int = 0,
) -> None:
    """Update a WebhookLog entry with the final status."""
    try:
        async with sessionmanager.session() as db:
            log_row = await db.get(WebhookLog, log_id)
            if log_row:
                log_row.status = status  # type: ignore[assignment]
                if agent_response:
                    log_row.agent_response = agent_response  # type: ignore[assignment]
                if error_message:
                    log_row.error_message = error_message[:2000]  # type: ignore[assignment]
                log_row.duration_ms = duration_ms  # type: ignore[assignment]
                await db.commit()
    except Exception as exc:
        logger.error(
            "[WEBHOOK BG] Failed to update log entry %s: %s", log_id, exc
        )


async def create_webhook_notification(
    user_id: int,
    *,
    title: str,
    message: str,
    link: str,
    metadata: dict,
    notif_type: str = "webhook",
) -> None:
    """Create a Notification row and push it via SSE."""
    try:
        async with sessionmanager.session() as db:
            notification = Notification(
                user_id=user_id,
                title=title,
                message=message,
                type=notif_type,
                link=link,
                metadata_json=json.dumps(metadata),
                is_read=False,
            )
            db.add(notification)
            await db.commit()
            await db.refresh(notification)

            await push_notification(user_id, {
                "id": notification.id,
                "title": notification.title,
                "message": notification.message,
                "type": notification.type,
                "link": notification.link,
                "is_read": False,
                "created_at": (
                    notification.created_at.isoformat()
                    if notification.created_at
                    else None
                ),
            })
    except Exception as exc:
        logger.error(
            "[WEBHOOK BG] Failed to create notification for user_id=%s: %s",
            user_id,
            exc,
        )

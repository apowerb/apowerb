"""Agent-bound ``get_webhook_backlog_status`` tool factory.

Lets the agent see what's still queued behind the row it is currently
processing — pending count, top of the FIFO, recent throughput. Used
by webhook-driven agents (SCEI ARs, future analogues) to mention the
backlog state in their final response so the operator knows whether
to wait for more output or start triaging manually.

The tool is bound to a specific ``agent_id`` at build time (via
``to_agent`` → ``bind_get_backlog_status``) so the agent cannot peek
into another agent's queue. The placeholder in
``tools_store/portfolio/basic.py`` returns an error when called
without the binding so a misconfiguration surfaces loudly instead of
silently leaking other agents' rows.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select

from apowerb.configs.th2logger import setup_logging
from apowerb.helpers.database import sessionmanager
from apowerb.models import WebhookLog


logger = setup_logging(__name__)


# Hard cap on how many pending rows the tool returns in detail. The
# agent should not need more than the next handful — the count alone
# carries the "still N to go" signal.
_PENDING_PREVIEW_LIMIT = 10


def _make_get_backlog_status(agent_id: int):
    """Return a ``get_webhook_backlog_status`` tool bound to ``agent_id``.

    The closure captures the integer id so the runtime function
    receives no agent-scoped argument from the LLM (one fewer thing
    the model can hallucinate).
    """

    async def get_webhook_backlog_status() -> dict[str, Any]:
        """Report what's queued behind this webhook-triggered run.

        Use this near the end of your reply when you've been invoked by
        a webhook so the operator knows whether more emails will arrive
        on their own. Counts are scoped to the current agent only; the
        tool cannot reveal another agent's queue.

        Returns:
            dict: ``{
              "agent_id": int,
              "current": {id, subject, sender, started_at} | None,
              "pending_count": int,
              "retrying_count": int,
              "pending": [{id, subject, sender, age_seconds}, ...],
              "completed_today": int,
              "failed_today": int
            }``
        """
        try:
            now = datetime.now(timezone.utc)
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            async with sessionmanager.session() as db:
                # The single in_progress row for this agent — the one
                # the worker is actively running. There can only ever be
                # one because the worker serialises agent runs.
                current_row = (
                    await db.execute(
                        select(WebhookLog)
                        .where(
                            WebhookLog.agent_id == agent_id,
                            WebhookLog.status == WebhookLog.STATUS_IN_PROGRESS,
                        )
                        .order_by(WebhookLog.started_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()

                pending_count = (
                    await db.execute(
                        select(func.count(WebhookLog.id)).where(
                            WebhookLog.agent_id == agent_id,
                            WebhookLog.status == WebhookLog.STATUS_PENDING,
                        )
                    )
                ).scalar_one() or 0

                retrying_count = (
                    await db.execute(
                        select(func.count(WebhookLog.id)).where(
                            WebhookLog.agent_id == agent_id,
                            WebhookLog.status == WebhookLog.STATUS_RETRYING,
                        )
                    )
                ).scalar_one() or 0

                completed_today = (
                    await db.execute(
                        select(func.count(WebhookLog.id)).where(
                            WebhookLog.agent_id == agent_id,
                            WebhookLog.status == WebhookLog.STATUS_SUCCESS,
                            WebhookLog.completed_at >= today_start,
                        )
                    )
                ).scalar_one() or 0

                failed_today = (
                    await db.execute(
                        select(func.count(WebhookLog.id)).where(
                            WebhookLog.agent_id == agent_id,
                            WebhookLog.status == WebhookLog.STATUS_ERROR,
                            WebhookLog.completed_at >= today_start,
                        )
                    )
                ).scalar_one() or 0

                pending_preview_rows = (
                    await db.execute(
                        select(WebhookLog)
                        .where(
                            WebhookLog.agent_id == agent_id,
                            WebhookLog.status.in_(
                                (
                                    WebhookLog.STATUS_PENDING,
                                    WebhookLog.STATUS_RETRYING,
                                )
                            ),
                        )
                        .order_by(WebhookLog.id.asc())
                        .limit(_PENDING_PREVIEW_LIMIT)
                    )
                ).scalars().all()

            current_payload = None
            if current_row is not None:
                current_payload = {
                    "id": current_row.id,
                    "subject": current_row.email_subject,
                    "sender": current_row.email_sender,
                    "started_at": (
                        current_row.started_at.isoformat()
                        if current_row.started_at is not None
                        else None
                    ),
                    "attempts": current_row.attempts,
                }

            pending_payload = []
            for r in pending_preview_rows:
                created = r.created_at
                age = None
                if created is not None:
                    if created.tzinfo is None:
                        # SQLite drops tzinfo — treat as UTC.
                        created = created.replace(tzinfo=timezone.utc)
                    age = int((now - created).total_seconds())
                pending_payload.append(
                    {
                        "id": r.id,
                        "subject": r.email_subject,
                        "sender": r.email_sender,
                        "status": r.status,
                        "attempts": r.attempts,
                        "age_seconds": age,
                    }
                )

            return {
                "status": "success",
                "agent_id": agent_id,
                "current": current_payload,
                "pending_count": int(pending_count),
                "retrying_count": int(retrying_count),
                "pending": pending_payload,
                "completed_today": int(completed_today),
                "failed_today": int(failed_today),
            }
        except Exception as exc:
            logger.exception(
                "[backlog_status_tool] query failed for agent_id=%s",
                agent_id,
            )
            return {
                "status": "error",
                "message": (
                    "Backlog query failed — surface this to the operator "
                    f"and continue without it. ({type(exc).__name__})"
                ),
            }

    # Preserve a stable __name__ for ADK's function-name dedup.
    get_webhook_backlog_status.__name__ = "tool_get_webhook_backlog_status"
    return get_webhook_backlog_status

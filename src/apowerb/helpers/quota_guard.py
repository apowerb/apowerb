"""Quota guard at the entry of an agent run.

The check happens BEFORE the response starts: cutting an SSE stream
mid-flight would leave a truncated conversation and a half-billed turn.
An overage therefore results in a clean refusal, never a cutoff.

Only concerns agents on the shared `thaink2/default` model: a personal
API key is paid for by its owner, it has no cap.
"""
from __future__ import annotations

from logging import getLogger
from typing import Optional

from fastapi import HTTPException
from fastapi import status as http_status

logger = getLogger(__name__)

# Business code returned to the frontend, which displays the message and
# the "Upgrade" button. 402 rather than 429: this isn't a rate limit you
# wait out for a few seconds, it's a credit exhausted until next month.
QUOTA_EXCEEDED_CODE = "QUOTA_EXCEEDED"


def agent_uses_default_llm(agent_name: str) -> bool:
    """True if this agent runs on the shared model.

    Best-effort: when in doubt (agent not found, DB silent) we return
    False, so it LETS THE RUN THROUGH. A quota guard must never block a
    conversation because of its own failure.

    Warning: only the called agent is inspected. A container agent
    (sequential/parallel) whose SUB-agents alone are on `thaink2/default`
    is not intercepted here -- its consumption is still recorded (the
    recorder runs on every sub-agent), so it will be capped on the next run.
    """
    try:
        from apowerb.core.agent_helpers.agent_utils import get_agent_details
        from apowerb.core.agent_helpers.default_llm import is_default_llm_model

        numeric_id = int(str(agent_name).replace("agent", "").strip())
        details = get_agent_details(agent_id=numeric_id)
        return is_default_llm_model(details.get("agent_model"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[QUOTA] could not resolve agent %s: %s", agent_name, exc)
        return False


async def enforce_run_quota(
    agent_name: str, owner_id: str, plan: Optional[str]
) -> None:
    """Raises a 402 if *owner_id* has exhausted their shared monthly quota.

    End-to-end best-effort: any failure of the check lets the run through.
    Losing a cap is less serious than making the product mute.
    """
    try:
        if not agent_uses_default_llm(agent_name):
            return

        from apowerb.core.usage_quota import get_quota_status
        from apowerb.helpers.database import sessionmanager

        async with sessionmanager.session() as db:
            status = await get_quota_status(db, owner_id=owner_id, plan=plan)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("[QUOTA] check unavailable for %s: %s", owner_id, exc)
        return

    if not status.exceeded:
        return

    logger.info(
        "[QUOTA] run refused for %s: %s/%s tokens this month",
        owner_id,
        status.used_tokens,
        status.limit_tokens,
    )
    raise HTTPException(
        status_code=http_status.HTTP_402_PAYMENT_REQUIRED,
        detail={
            "code": QUOTA_EXCEEDED_CODE,
            "message": (
                "Vous avez atteint votre quota mensuel de tokens inclus. "
                "Il sera renouvele au debut du mois prochain."
            ),
            "used_tokens": status.used_tokens,
            "limit_tokens": status.limit_tokens,
            "resets_at": status.resets_at.isoformat() if status.resets_at else None,
        },
    )

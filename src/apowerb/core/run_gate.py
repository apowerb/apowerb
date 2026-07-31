"""Mandatory checkpoint for every agent run, in front of the registered guards.

There are several entry doors into a run: chat (`/api/adk/run`,
`/api/adk/run_sse`), scheduled runs (Mage/th2etl via `/run_from_jwt` and
`/run_from_refresh_token`), and webhooks. Each one used to resolve -- or
forget to resolve -- the guards on its own: only the first two did, the
others slipped through.

This module is the single choke point. Any new door must call it; a
coverage test (`tests/test_run_gate_couverture.py`) re-reads the sources and
fails if a module calls the ADK runner without going through here.

The core names no guard itself: it asks the extension registry for them.
With no add-on installed, the list is empty and everything passes through.
"""
from __future__ import annotations

from logging import getLogger
from typing import Optional

logger = getLogger(__name__)


async def resolve_owner_plan(owner_id: str) -> Optional[str]:
    """Commercial plan for *owner_id* (their email), or ``None``.

    Best-effort: non-interactive paths (scheduled, webhook) don't have a
    user object at hand, only an identifier. A read failure yields ``None``,
    which falls back the guard to the default cap -- never to "no cap".
    """
    if not owner_id:
        return None
    try:
        from sqlalchemy import select

        from apowerb.helpers.database import sessionmanager
        from apowerb.models import User

        async with sessionmanager.session() as db:
            result = await db.execute(
                select(User.plan).where(User.email == owner_id)
            )
            return result.scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[RUN GATE] plan unreadable for %s: %s", owner_id, exc
        )
        return None


async def apply_run_guards(
    *, agent_name: str, owner_id: str, plan: Optional[str]
) -> None:
    """Put an agent run through every registered guard.

    Raises whatever a guard raises (typically a 402 for quota exceeded):
    that's the hard refusal, before anything even starts.

    An empty ``owner_id`` does NOT silently skip the check: it is logged
    as a WARNING. A commercial cap should fail open rather than make the
    product mute, but the opening must be audible -- otherwise it becomes
    the next hole.
    """
    from apowerb.core.extensions.registry import registry

    guards = registry.run_guards()
    if not guards:
        return

    if not owner_id:
        logger.warning(
            "[RUN GATE] run for %s with no owner resolved: %d guard(s) "
            "not applied",
            agent_name,
            len(guards),
        )
        return

    for guard in guards:
        await guard(agent_name, owner_id=owner_id, plan=plan)

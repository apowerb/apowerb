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

from datetime import datetime, timedelta, timezone
from logging import getLogger
from typing import Optional

from fastapi import HTTPException

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

    # Le plafond du noyau passe AVANT les gardes de briques, et il passe
    # meme quand il n'y en a aucune : c'est toute la difference entre un
    # plafond installable et un plafond installe.
    await _core_token_cap(agent_name=agent_name, owner_id=owner_id)

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


# ---------------------------------------------------------------------------
# Plafond de jetons du noyau
# ---------------------------------------------------------------------------


def _refus(scope: str, used: int, cap: int, window_hours: int, retry_s: int) -> HTTPException:
    """402, avec de quoi ecrire un message utile plutot qu'un mur muet."""
    return HTTPException(
        status_code=402,
        detail={
            "code": "TOKEN_QUOTA_EXCEEDED",
            "scope": scope,
            "used": used,
            "cap": cap,
            "window_hours": window_hours,
            "retry_after_seconds": retry_s,
        },
    )


def _as_utc(value: datetime) -> datetime:
    """SQLite rend des datetimes naifs la ou Postgres les rend aware."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def _core_token_cap(*, agent_name: str, owner_id: str) -> None:
    """Borner la consommation du modele mutualise, par compte et globalement.

    Ne s'applique qu'a ``billed_to_thaink2`` : une cle API personnelle est
    payee par celui qui la fournit.

    S'efface devant une brique qui a rempli le crochet DEDIE
    (``register_default_llm_cap``) -- pas devant une garde quelconque, sinon
    une extension sans rapport desarmerait le plafond sans le dire.

    Une base illisible laisse passer, en le disant fort : rendre le produit
    muet sur un hoquet serait pire que le depassement qu'on evite. Meme
    regle que le reste de ce module.
    """
    from apowerb.configs.settings import get_settings
    from apowerb.core.extensions.registry import registry

    if registry.default_llm_cap() is not None:
        return

    settings = get_settings()
    per_user = max(0, int(getattr(settings, "default_llm_user_token_cap", 0) or 0))
    overall = max(0, int(getattr(settings, "default_llm_global_token_cap", 0) or 0))
    if not per_user and not overall:
        return

    window_h = max(1, int(getattr(settings, "default_llm_cap_window_hours", 24) or 24))
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=window_h)

    try:
        from sqlalchemy import func, select

        from apowerb.helpers.database import sessionmanager
        from apowerb.models import LlmUsage

        conditions = [
            LlmUsage.billed_to_thaink2.is_(True),
            LlmUsage.created_at >= since,
        ]

        async with sessionmanager.session() as db:

            async def consomme(*extra) -> tuple[int, Optional[datetime]]:
                """Jetons sur la fenetre, et l'horodatage du plus ancien."""
                row = (
                    await db.execute(
                        select(
                            func.coalesce(func.sum(LlmUsage.total_tokens), 0),
                            func.min(LlmUsage.created_at),
                        ).where(*conditions, *extra)
                    )
                ).one()
                return int(row[0] or 0), row[1]

            def recharge(oldest: Optional[datetime]) -> int:
                """Secondes avant que du budget se libere.

                L'echeance est la sortie de fenetre de la plus ancienne ligne
                comptee : c'est le premier instant ou le total peut baisser.
                """
                if oldest is None:
                    return window_h * 3600
                delta = (_as_utc(oldest) + timedelta(hours=window_h)) - now
                return max(1, int(delta.total_seconds()))

            if per_user and owner_id:
                used, oldest = await consomme(LlmUsage.owner_id == owner_id)
                if used >= per_user:
                    logger.warning(
                        "[RUN GATE] %s a atteint son plafond de jetons "
                        "(%d/%d sur %d h) -- run %s refuse",
                        owner_id, used, per_user, window_h, agent_name,
                    )
                    raise _refus("user", used, per_user, window_h, recharge(oldest))

            if overall:
                used, oldest = await consomme()
                if used >= overall:
                    logger.warning(
                        "[RUN GATE] plafond de jetons du DEPLOIEMENT atteint "
                        "(%d/%d sur %d h) -- run %s refuse",
                        used, overall, window_h, agent_name,
                    )
                    raise _refus("deployment", used, overall, window_h, recharge(oldest))

    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[RUN GATE] plafond de jetons NON applique (consommation "
            "illisible) pour %s : %s",
            agent_name, exc,
        )

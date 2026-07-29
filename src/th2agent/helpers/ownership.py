"""Helpers centralisés pour valider l'ownership (IDOR).

Évite les imports circulaires inter-routers et regroupe les checks d'ownership
communs (agent_id → owner_id == current_user.email).
"""

import asyncio
import re
from logging import getLogger

from fastapi import HTTPException

from th2agent.users import schemas as user_schemas

logger = getLogger(__name__)


async def validate_agent_ownership(agent_id: str, current_user: user_schemas.User) -> None:
    """Vérifie le format, l'existence et l'appartenance d'un agent.

    Raises:
        HTTPException 400 – format invalide (protection path traversal)
        HTTPException 404 – agent inexistant
        HTTPException 403 – agent d'un autre utilisateur
    """
    if not re.match(r"^agent\d+$", agent_id):
        logger.warning(
            "[OWNERSHIP] Invalid agent_id format: %r from user %s",
            agent_id, current_user.email,
        )
        raise HTTPException(status_code=400, detail="Invalid agent_id format")

    # Lazy import pour éviter tout import circulaire
    from th2agent.core.agent_main import agent_store

    numeric_id = int(agent_id.replace("agent", ""))

    select_query = agent_store.agent_table.select().where(
        agent_store.agent_table.c.agent_id == numeric_id,
    )
    rows = await asyncio.to_thread(agent_store.get_list_agents, select_query)
    if not rows:
        raise HTTPException(status_code=404, detail="Agent not found")

    agent = rows[0]._asdict()
    if str(agent.get("owner_id")) != str(current_user.email):
        logger.warning(
            "[OWNERSHIP] Denied: user %s tried to access agent %s (owner=%s)",
            current_user.email, agent_id, agent.get("owner_id"),
        )
        raise HTTPException(status_code=403, detail="Not your agent")


def enforce_user_id_match(requested_user_id: str | None, current_user: user_schemas.User) -> None:
    """Reject a request whose `user_id` differs from the authenticated user.

    Used on endpoints that accept `user_id` in the path or body (ADK runner,
    artefacts, etc.) to prevent IDOR.

    Raises HTTP 403 on mismatch; no-op if `requested_user_id` is None.
    """
    if requested_user_id is None:
        return
    if str(requested_user_id) != str(current_user.email):
        logger.warning(
            "[OWNERSHIP] IDOR denied: %s attempted access with user_id=%s",
            current_user.email, requested_user_id,
        )
        raise HTTPException(
            status_code=403,
            detail="Forbidden: user_id does not match authenticated user",
        )

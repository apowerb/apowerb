"""Helpers centralisés pour valider l'ownership (IDOR).

Évite les imports circulaires inter-routers et regroupe les checks d'ownership
communs (agent_id → owner_id == current_user.email).

Ces fonctions vivaient dans `evaluation/run_service.py`. L'évaluation devient
une brique commerciale, et ce sont des questions de *possession*, pas
d'évaluation : « quels agents sont les miens », « qui peut lire le compte d'un
autre ». Elles restent donc dans le noyau, où la supervision et n'importe quel
autre consommateur peuvent les appeler sans dépendre d'un paquet qui déménage.
"""

import asyncio
import re
from logging import getLogger

from fastapi import HTTPException

from sqlalchemy.ext.asyncio import AsyncSession

from apowerb.models import UserRole
from apowerb.users import schemas as user_schemas

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
    from apowerb.core.agent_main import agent_store

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


def is_admin(user) -> bool:
    """`auth.dependencies` fills `role` from `UserRole.value` -- "ADMIN",
    upper case. Comparing against "admin" silently never matched, so the
    bypass below never fired. Normalise rather than trust one spelling.
    """
    return str(getattr(user, "role", "") or "").upper() == UserRole.ADMIN.value

async def owned_agent_ids(db: AsyncSession, current_user: user_schemas.User) -> set[int] | None:
    """None means unrestricted (admin). Otherwise the exact set of
    agent_ids this user owns -- callers must apply it when building the
    query, never after fetching rows.
    """
    if is_admin(current_user):
        return None

    from apowerb.core.agent_main import agent_store

    select_query = (
        agent_store.agent_table.select()
        .where(agent_store.agent_table.c.owner_id == current_user.email)
        .with_only_columns(agent_store.agent_table.c.agent_id)
    )
    rows = await asyncio.to_thread(agent_store.get_list_agents, select_query)
    return {row[0] for row in rows}

async def may_supervise_across_accounts(db, user) -> bool:
    """May this user read other people's sessions?

    The core cannot answer on its own: "superadmin" is a row in a table
    that belongs to a commercial brick, and the core never names one. It
    asks the registry, and with no brick registered the answer is no —
    every install then shows each person their own sessions, which is the
    only default the core can honestly hold.
    """
    from apowerb.core.extensions.registry import registry

    resolver = registry.supervision_scope()
    if resolver is None:
        return False
    return bool(await resolver(db, user))

async def list_owned_agents(
    current_user: user_schemas.User,
    *,
    admin_sees_all: bool,
) -> list[tuple[int, str, str | None]]:
    """Every agent this user owns as `(agent_id, agent_name, owner_id)`.

    `admin_sees_all` says whether an administrator gets the whole platform
    or only their own agents, and it has no default on purpose. The two
    callers want opposite things: Supervision is an admin screen and
    crossing accounts is its job, while Evaluations is a product screen
    where it meant listing every agent on the platform next to its owner's
    email address. A default would let the next route inherit whichever
    answer happened to be written here.

    Carries the display name along so `GET /evaluations/agents` never has
    to look an agent's name up one at a time (the exact N+1 shape already
    paid for once on the Artefacts screen). One query on `agent_store`'s
    own synchronous connection, independent of the number of agents
    returned.
    """
    from apowerb.core.agent_main import agent_store

    select_query = agent_store.agent_table.select().with_only_columns(
        agent_store.agent_table.c.agent_id,
        agent_store.agent_table.c.agent_name,
        # An admin gets every agent, so the caller has to be able to say
        # whose each one is -- without this the screen can only claim they
        # are all yours, which is what it did.
        agent_store.agent_table.c.owner_id,
    )
    if not (admin_sees_all and is_admin(current_user)):
        select_query = select_query.where(
            agent_store.agent_table.c.owner_id == current_user.email
        )
    rows = await asyncio.to_thread(agent_store.get_list_agents, select_query)
    return [(row[0], row[1], row[2]) for row in rows]

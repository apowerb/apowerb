"""Keep every worker's cached agents in step with the database.

A write invalidates the agent's cached module and runner, but only in the
process that served it: each uvicorn worker, each replica, holds its own
``ApiServer.runner_dict`` and ``AgentLoader`` cache. Behind more than one of
them, an edited agent kept answering with its old definition wherever the edit
did not land, a deleted one kept running, and an agent created on one replica
had no module on the others until their next restart.

Before handing out a runner, each worker now reads the agent's fingerprint --
its row and those of its sub-agents, which the runner embeds -- and rebuilds
when it differs from the one the cached runner was built from. The database
stays the only source of truth: no broker, no message to lose. The price is
two indexed reads per level of sub-agents, against seconds of LLM time per run.
"""

from __future__ import annotations

import asyncio
import functools
import re
from logging import getLogger
from typing import Any, Callable, Hashable

from fastapi import HTTPException
from google.adk.cli.utils import cleanup

from apowerb.core.adk_agent_builder import ensure_agent_module

logger = getLogger(__name__)

_APP_NAME = re.compile(r"agent(\d+)")
_SUB_AGENT = re.compile(r"(?:agent)?(\d+)")

# Fingerprint each cached runner was built from, per ApiServer instance.
_BUILT_FROM = "_apowerb_built_from"


def drop_cached_agent(adk_server: Any, app_name: str) -> None:
    """Drop the cached module and runner of ``app_name`` in this process.

    The next ``get_runner_async(app_name)`` re-imports the module, whose
    ``to_agent()`` reads the definition from the database again.
    """
    try:
        adk_server.agent_loader.remove_agent_from_cache(app_name)
    except Exception as exc:
        logger.warning(
            "[agent-reload] remove_agent_from_cache(%s) raised %s: %s",
            app_name,
            type(exc).__name__,
            exc,
        )
    # Only queue the runner for cleanup if one actually exists: ADK's
    # close_runners([None]) crashes with ``'NoneType' object has no
    # attribute 'close'`` otherwise. When the agent has never been
    # instantiated in this process, dropping the module cache is already
    # enough -- the next request builds a fresh runner from scratch.
    try:
        runner_dict = getattr(adk_server, "runner_dict", None) or {}
        if app_name in runner_dict:
            adk_server.runners_to_clean.add(app_name)
    except Exception as exc:
        logger.warning(
            "[agent-reload] could not mark runner %s for cleanup: %s", app_name, exc
        )


def agent_fingerprint(agent_id: int) -> Hashable | None:
    """What a runner built for ``agent_id`` depends on, or None if it is gone.

    ``updated_at`` alone has a one-second resolution; every update, resync and
    restore also archives a revision, whose id only grows. Sub-agents are
    followed because ``to_agent`` builds them into the parent's runner.
    """
    from sqlalchemy import func, select

    from apowerb.core.agent_main import _parse_string_list, agent_store

    agents, revisions = agent_store.agent_table, agent_store.revision_table
    fingerprint: list[tuple] = []
    seen: set[int] = set()
    level = {agent_id}
    with agent_store.engine.connect() as conn:
        while level:
            seen |= level
            rows = conn.execute(
                select(agents.c.agent_id, agents.c.updated_at, agents.c.sub_agents)
                .where(agents.c.agent_id.in_(level))
            ).all()
            if agent_id in level and all(r.agent_id != agent_id for r in rows):
                return None
            latest = dict(
                conn.execute(
                    select(revisions.c.agent_id, func.max(revisions.c.revision_id))
                    .where(revisions.c.agent_id.in_(level))
                    .group_by(revisions.c.agent_id)
                ).all()
            )
            level = set()
            for row in rows:
                fingerprint.append((row.agent_id, row.updated_at, latest.get(row.agent_id)))
                for name in _parse_string_list(row.sub_agents):
                    match = _SUB_AGENT.fullmatch(str(name))
                    if match and int(match[1]) not in seen:
                        level.add(int(match[1]))
    return tuple(sorted(fingerprint, key=lambda entry: entry[0]))


def keep_runners_in_sync(
    get_runner_async: Callable,
    fingerprint: Callable[[int], Hashable | None] = agent_fingerprint,
) -> Callable:
    """Wrap ``ApiServer.get_runner_async`` so it never serves a stale agent."""

    @functools.wraps(get_runner_async)
    async def get_fresh_runner_async(self, app_name: str):
        match = _APP_NAME.fullmatch(app_name)
        if match is None:
            return await get_runner_async(self, app_name)
        agent_id = int(match[1])

        try:
            current = await asyncio.to_thread(fingerprint, agent_id)
        except Exception as exc:
            # Refusing every run while the database blips would be worse than
            # serving what this worker already has.
            logger.warning(
                "[agent-sync] fingerprint of %s unreadable (%s: %s) -- serving the cached runner",
                app_name,
                type(exc).__name__,
                exc,
            )
            return await get_runner_async(self, app_name)

        built_from = self.__dict__.setdefault(_BUILT_FROM, {})
        if current is None:
            # ADK only closes a stale runner on the next build, which a deleted
            # agent never gets: close it here.
            drop_cached_agent(self, app_name)
            self.runners_to_clean.discard(app_name)
            stale = self.runner_dict.pop(app_name, None)
            if stale is not None:
                await cleanup.close_runners([stale])
            built_from.pop(app_name, None)
            raise HTTPException(status_code=404, detail=f"Agent not found: {app_name}")

        if app_name in built_from and built_from[app_name] != current:
            logger.info("[agent-sync] %s changed in the database -- rebuilding", app_name)
            drop_cached_agent(self, app_name)
        ensure_agent_module(agent_id, self.agent_loader.agents_dir)

        runner = await get_runner_async(self, app_name)
        built_from[app_name] = current
        return runner

    get_fresh_runner_async.keeps_agents_in_sync = True
    return get_fresh_runner_async

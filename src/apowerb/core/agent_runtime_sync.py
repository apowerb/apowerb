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

Retiring the old runner is deferred. ADK closes a replaced runner on the next
``get_runner_async`` (via ``runners_to_clean``), which closes its toolsets --
and any live MCP session -- even while a run started before the edit is still
iterating that same runner. Closing the session mid-call makes ADK retry the
tool, so a stdio/remote MCP tool runs twice. Instead, each runner counts its
in-flight ``run_async`` calls; a replaced or deleted runner is detached from
the cache at once but closed only once its last run has finished.
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

# Per-runner bookkeeping for deferred close (instance attributes on Runner).
_ACTIVE = "_apowerb_active_runs"  # in-flight run_async calls
_RETIRED = "_apowerb_retired"  # detached from cache, close when idle
_WRAPPED = "_apowerb_run_wrapped"  # run_async already instrumented


async def _close_runner_safely(runner: Any) -> None:
    """Close a retired runner, swallowing and logging any error."""
    try:
        await asyncio.wait_for(cleanup.close_runners([runner]), timeout=10)
    except Exception as exc:
        logger.warning(
            "[agent-sync] closing a retired runner raised %s: %s",
            type(exc).__name__,
            exc,
        )


def _schedule_close(runner: Any) -> None:
    """Close ``runner`` now, from sync code, whether or not a loop is running."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        loop.create_task(_close_runner_safely(runner))
    else:
        try:
            asyncio.run(_close_runner_safely(runner))
        except Exception as exc:
            logger.warning(
                "[agent-sync] closing a retired runner failed: %s", exc
            )


def _instrument_runner(runner: Any) -> None:
    """Count in-flight ``run_async`` calls so a retired runner closes when idle.

    Idempotent: a cached runner handed out again keeps its single wrapper.
    ``Runner`` is a plain class, so instance-level assignment shadows the
    method for this object only. ``run_live`` is not wrapped -- a websocket
    stream cut by a rebuild is a separate, rarer path.
    """
    if runner is None or getattr(runner, _WRAPPED, False):
        return
    original = runner.run_async
    setattr(runner, _ACTIVE, 0)
    setattr(runner, _RETIRED, False)

    @functools.wraps(original)
    async def _counted_run_async(*args: Any, **kwargs: Any):
        setattr(runner, _ACTIVE, getattr(runner, _ACTIVE, 0) + 1)
        try:
            async for event in original(*args, **kwargs):
                yield event
        finally:
            remaining = getattr(runner, _ACTIVE, 1) - 1
            setattr(runner, _ACTIVE, remaining)
            if remaining <= 0 and getattr(runner, _RETIRED, False):
                await _close_runner_safely(runner)

    runner.run_async = _counted_run_async
    setattr(runner, _WRAPPED, True)


def _retire_runner(runner: Any) -> None:
    """Detach done: close ``runner`` now if idle, else when its last run ends."""
    if runner is None:
        return
    if getattr(runner, _ACTIVE, 0) > 0:
        setattr(runner, _RETIRED, True)
        return
    _schedule_close(runner)


def drop_cached_agent(adk_server: Any, app_name: str) -> None:
    """Drop the cached module and runner of ``app_name`` in this process.

    The next ``get_runner_async(app_name)`` re-imports the module, whose
    ``to_agent()`` reads the definition from the database again. The old
    runner is detached from the cache and closed only once no run is still
    using it (see module docstring).
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
    # Detach and retire the runner ourselves rather than queueing it in
    # ``runners_to_clean``: ADK would close it on the next get_runner_async,
    # tearing down the toolsets of a runner a live run may still hold.
    try:
        runner_dict = getattr(adk_server, "runner_dict", None)
        if runner_dict and app_name in runner_dict:
            old = runner_dict.pop(app_name)
            try:
                adk_server.runners_to_clean.discard(app_name)
            except Exception:
                pass
            _retire_runner(old)
    except Exception as exc:
        logger.warning(
            "[agent-reload] could not retire runner %s: %s", app_name, exc
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
            runner = await get_runner_async(self, app_name)
            _instrument_runner(runner)
            return runner

        built_from = self.__dict__.setdefault(_BUILT_FROM, {})
        if current is None:
            # ADK only closes a stale runner on the next build, which a deleted
            # agent never gets: detach and retire it here (closed once idle).
            drop_cached_agent(self, app_name)
            built_from.pop(app_name, None)
            raise HTTPException(status_code=404, detail=f"Agent not found: {app_name}")

        if app_name in built_from and built_from[app_name] != current:
            logger.info("[agent-sync] %s changed in the database -- rebuilding", app_name)
            drop_cached_agent(self, app_name)
        ensure_agent_module(agent_id, self.agent_loader.agents_dir)

        runner = await get_runner_async(self, app_name)
        _instrument_runner(runner)
        built_from[app_name] = current
        return runner

    get_fresh_runner_async.keeps_agents_in_sync = True
    return get_fresh_runner_async

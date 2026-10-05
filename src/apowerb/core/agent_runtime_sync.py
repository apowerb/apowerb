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
in-flight ``run_async`` and ``run_live`` calls; a replaced or deleted runner is
detached from the cache at once but closed only once its last run has finished.
"""

from __future__ import annotations

import asyncio
import functools
import re
from logging import getLogger
from typing import Any, Callable, Hashable, cast

from fastapi import HTTPException
from google.adk.cli.utils import cleanup

from apowerb.core.adk_agent_builder import ensure_agent_module

logger = getLogger(__name__)

_APP_NAME = re.compile(r"agent(\d+)")
_SUB_AGENT = re.compile(r"(?:agent)?(\d+)")

# Fingerprint each cached runner was built from, per ApiServer instance.
_BUILT_FROM = "_apowerb_built_from"

# Per-runner bookkeeping for deferred close (instance attributes on Runner).
_ACTIVE = "_apowerb_active_runs"  # in-flight run_async + run_live calls
_RETIRED = "_apowerb_retired"  # detached from cache, close when idle
_WRAPPED = "_apowerb_run_wrapped"  # run_async/run_live already instrumented
_CLOSED = "_apowerb_closed"  # close already started -- never close twice
_RUN_METHODS = ("run_async", "run_live")

# A runner retired while idle is not closed at once: the run it was handed out
# for may not have started yet. ``get_runner_async`` returns the runner, and
# only its first ``run_async`` iteration bumps ``_ACTIVE`` -- in between, the
# SSE route awaits ``get_session`` (api_server.py ``/run_sse``) with the runner
# still at ``_ACTIVE == 0``. Closing it then would tear its toolsets (and any
# live MCP session) out from under a run about to begin. We wait this long and
# re-check: by then the run has either started (``_ACTIVE > 0``, its own finally
# closes it) or it never will (a handout whose caller errored out -- closed to
# avoid a leak). The window is measured (that one await); the grace covering it
# is a generous estimate of a session lookup, not a proof. Monkeypatched small
# in tests.
_RUNNER_CLOSE_GRACE = 30.0  # seconds


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


def _claim_close(runner: Any) -> bool:
    """Set the close-once flag, returning True only for the first caller.

    Both the grace timer and a run's ``finally`` may reach an idle retired
    runner; on one event loop this check-and-set has no await between the read
    and the write, so exactly one of them closes it.
    """
    if getattr(runner, _CLOSED, False):
        return False
    setattr(runner, _CLOSED, True)
    return True


def _close_now(runner: Any) -> None:
    """Close ``runner`` once, from sync code, whether or not a loop is running."""
    if not _claim_close(runner):
        return
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
            logger.warning("[agent-sync] closing a retired runner failed: %s", exc)


def _close_if_idle(runner: Any) -> None:
    """Grace-timer callback: close the runner only if no run has started."""
    if getattr(runner, _ACTIVE, 0) > 0:
        return  # a run began during the grace -- its finally will close it
    _close_now(runner)  # _claim_close makes this a no-op if already closed


def _instrument_runner(runner: Any) -> None:
    """Count in-flight runs so a retired runner closes when idle.

    Both ``run_async`` (HTTP/SSE) and ``run_live`` (the ``/run_live``
    WebSocket) are wrapped and share one ``_ACTIVE`` counter, so a runner with
    either kind of run still streaming is closed only once the last one ends --
    a rebuild must not tear a live MCP session out from under a WebSocket any
    more than from under an HTTP run.

    Idempotent: a cached runner handed out again keeps its single wrapper.
    ``Runner`` is a plain class, so instance-level assignment shadows the
    method for this object only.
    """
    if runner is None or getattr(runner, _WRAPPED, False):
        return
    setattr(runner, _ACTIVE, 0)
    setattr(runner, _RETIRED, False)

    def _wrap(method_name: str) -> None:
        original = getattr(runner, method_name, None)
        if original is None:  # a stand-in without this run method
            return

        @functools.wraps(original)
        async def _counted(*args: Any, **kwargs: Any):
            setattr(runner, _ACTIVE, getattr(runner, _ACTIVE, 0) + 1)
            try:
                async for event in original(*args, **kwargs):
                    yield event
            finally:
                remaining = getattr(runner, _ACTIVE, 1) - 1
                setattr(runner, _ACTIVE, remaining)
                if (
                    remaining <= 0
                    and getattr(runner, _RETIRED, False)
                    and _claim_close(runner)
                ):
                    await _close_runner_safely(runner)

        setattr(runner, method_name, _counted)

    for method_name in _RUN_METHODS:
        _wrap(method_name)
    setattr(runner, _WRAPPED, True)


def _retire_runner(runner: Any) -> None:
    """Detach done: close ``runner`` once no run holds it, after a grace window.

    Idempotent -- a runner retired twice schedules only one grace timer.
    """
    if runner is None or getattr(runner, _RETIRED, False):
        return
    setattr(runner, _RETIRED, True)
    if getattr(runner, _ACTIVE, 0) > 0:
        return  # a run is in flight; its finally closes it when it ends
    # Idle now, but possibly a runner just handed out whose run has not started
    # (see _RUNNER_CLOSE_GRACE). Defer the close and re-check after the grace.
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No loop in this thread. All retire paths are async today, so this is
        # only a defensive fallback: close now, as before the grace existed.
        _close_now(runner)
        return
    loop.call_later(_RUNNER_CLOSE_GRACE, _close_if_idle, runner)


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
        logger.warning("[agent-reload] could not retire runner %s: %s", app_name, exc)


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
                select(
                    agents.c.agent_id, agents.c.updated_at, agents.c.sub_agents
                ).where(agents.c.agent_id.in_(level))
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
                fingerprint.append(
                    (row.agent_id, row.updated_at, latest.get(row.agent_id))
                )
                for name in _parse_string_list(row.sub_agents):
                    match = _SUB_AGENT.fullmatch(str(name))
                    if match and int(match[1]) not in seen:
                        level.add(int(match[1]))
    return tuple(sorted(fingerprint, key=lambda entry: entry[0]))


def _as_agent_id(entry: Any) -> int | None:
    """The numeric agent id behind a run's ``agent_ids`` entry, or None.

    Entries are heterogeneous: a chat run stores the app name (``agent3``), a
    scheduled one the id itself. ``_SUB_AGENT`` accepts both ``agent3`` and
    ``3``; anything else (a free-form name, an overlay tool) yields None.
    """
    if isinstance(entry, bool):
        return None
    if isinstance(entry, int):
        return entry
    match = _SUB_AGENT.fullmatch(str(entry))
    return int(match[1]) if match else None


def agent_version_records(agent_ids: Any) -> dict[str, list]:
    """Each run agent's definition fingerprint, for the run-log audit trail.

    Answers "which agent definition produced this run": for every entry of
    ``agent_ids`` that resolves to an agent, records what ``agent_fingerprint``
    captures (each agent and sub-agent's ``updated_at`` and max ``revision_id``),
    made JSON-serialisable. Because ``keep_runners_in_sync`` rebuilds a runner
    whose fingerprint changed *before* handing it out, this DB fingerprint taken
    at run start is the definition the run actually executes.

    Best-effort and side-effect-free: an unresolvable entry or an unreadable
    fingerprint is skipped, a total failure returns ``{}``. It never raises, so
    recording a version can never break or stall a run.
    """
    records: dict[str, list] = {}
    try:
        entries = list(agent_ids or [])
    except TypeError:
        return records
    for entry in entries:
        agent_id = _as_agent_id(entry)
        if agent_id is None:
            continue
        try:
            fingerprint = agent_fingerprint(agent_id)
        except Exception as exc:  # noqa: BLE001 - audit must not break the run
            logger.warning(
                "[agent-version] fingerprint for agent %s unavailable: %s",
                agent_id,
                exc,
            )
            continue
        if fingerprint is None:
            continue
        # agent_fingerprint is typed Hashable (used as a cache key); at runtime
        # it is the tuple of (agent_id, updated_at, revision_id) rows built above.
        records[str(agent_id)] = [
            [aid, str(updated_at), revision_id]
            for (aid, updated_at, revision_id) in cast("tuple", fingerprint)
        ]
    return records


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
            logger.info(
                "[agent-sync] %s changed in the database -- rebuilding", app_name
            )
            drop_cached_agent(self, app_name)
        ensure_agent_module(agent_id, self.agent_loader.agents_dir)

        runner = await get_runner_async(self, app_name)
        _instrument_runner(runner)
        built_from[app_name] = current
        return runner

    get_fresh_runner_async.keeps_agents_in_sync = True
    return get_fresh_runner_async

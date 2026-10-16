"""Tool-config params in ``os.environ`` only while the tool that owns them runs.

Portfolio tools read their settings with ``os.getenv`` at call time. Writing a
config's params into the process env when the agent is built left them there
for every later call in the worker, whichever agent made it. Instead, each tool
function built from a tool_config is wrapped so its params are applied around
the call and restored afterwards.

One re-entrant lock serialises every such patch: ADK may run sync tools on the
event loop or in a thread pool, and BI sources run tools in worker threads.
"""
from __future__ import annotations

import functools
import inspect
import os
import threading
from contextlib import contextmanager
from logging import getLogger
from typing import Any, Callable, Iterable, Iterator

logger = getLogger(__name__)

TOOL_ENV_LOCK = threading.RLock()


def declared_env_keys(tool_name: str) -> tuple[str, ...]:
    """Env vars the tool's module reads (best effort; empty if unknown)."""
    return _declared_env_keys(tool_name.split(".")[0])


@functools.lru_cache(maxsize=256)
def _declared_env_keys(category: str) -> tuple[str, ...]:
    try:
        from apowerb.tools_store.tool_manager import get_tools_store

        return tuple(p["key"] for p in get_tools_store().get_tool_expected_params(f"{category}.x"))
    except Exception:  # noqa: BLE001
        return ()


def _apply(patch: dict[str, str | None]) -> None:
    for key, value in patch.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


@contextmanager
def tool_env(params: dict[str, Any], declared: Iterable[str] = ()) -> Iterator[None]:
    """Apply ``params`` to ``os.environ`` for the block, then restore it.

    Variables in ``declared`` that ``params`` does not set, and params set to
    None, are removed for the block, so the tool never falls back to a value
    another config left behind.
    """
    env = {str(k): None if v is None else str(v) for k, v in params.items()}
    patch: dict[str, str | None] = {**{k: None for k in declared}, **env}
    with TOOL_ENV_LOCK:
        saved = {k: os.environ.get(k) for k in patch}
        try:
            _apply(patch)
            yield
        finally:
            _apply(saved)


def with_tool_config(fn: Any, params: dict[str, Any], tool_name: str) -> Any:
    """Return ``fn`` wrapped so ``params`` are in the env only during each call.

    The wrapper keeps the name, docstring and signature ADK reads. Objects that
    are not plain sync functions are returned unchanged.
    """
    if not params or not (inspect.isfunction(fn) or inspect.ismethod(fn)):
        return fn
    if inspect.iscoroutinefunction(fn):
        # Holding the env across an await would mix concurrent requests on
        # the loop; no portfolio tool is async today.
        logger.warning("[TOOLS] async tool %s: tool_config params not applied", tool_name)
        return fn
    declared = declared_env_keys(tool_name)
    frozen = dict(params)

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        with tool_env(frozen, declared):
            return fn(*args, **kwargs)

    return wrapper


def call_with_tool_config(factory: Callable[[], Any], params: dict[str, Any], tool_name: str) -> Any:
    """Call a toolset factory that reads its settings when it is built."""
    if not params:
        return factory()
    with tool_env(params, declared_env_keys(tool_name)):
        return factory()

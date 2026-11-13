"""
bi/data/tool_executor.py
------------------------
Reads a dashboard source from an allowlisted agent tool (``source_type="tool"``).

The tool is resolved by name in the BI allowlist (``tools_store.bi_source``),
run as the dashboard owner (never the viewer) and must answer with a table:
a list of dicts, or ``{"rows": [...]}``. See ``tools_store/bi_source.py`` for
the contract.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
from typing import Any, Callable

from apowerb.tools_store.tool_env import TOOL_ENV_LOCK, declared_env_keys, tool_env
from apowerb.tools_store.bi_source import BiSourceError, credentials_of, resolve_bi_source
from apowerb.tools_store.tools_helpers import load_tool_config_params

logger = logging.getLogger(__name__)

_TIMEOUT_S = float(os.getenv("BI_TOOL_TIMEOUT_S", "60"))

# Tool configs reach tools through process-wide env vars: one call at a time
# may own them, or two owners' tokens would mix. Same lock as agent tools.
_ENV_LOCK = TOOL_ENV_LOCK

_FAILURE_STATUSES = {"error", "failed", "failure"}
_MESSAGE_KEYS = ("error", "error_message", "message")


class ToolSourceError(ValueError):
    """A tool source cannot be read; the message is shown to the author."""


def _failure_message(answer: dict[str, Any]) -> str | None:
    """The tool's own error message if ``answer`` reports a failure, else None."""
    status = str(answer.get("status", "")).lower()
    failed = answer.get("success") is False or status in _FAILURE_STATUSES
    if not failed and not (answer.get("error") and "rows" not in answer):
        return None
    for key in _MESSAGE_KEYS:
        msg = answer.get(key)
        if isinstance(msg, str) and msg.strip():
            return msg.strip()
    return "l'outil a signalé une erreur sans message."


def _flatten(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple, set)):
        return json.dumps(value, default=str, ensure_ascii=False)
    return value


def rows_from_answer(answer: Any, tool: str, max_rows: int) -> list[dict[str, Any]]:
    """Validate a tool answer and return its rows (nested values flattened)."""
    if isinstance(answer, dict):
        failure = _failure_message(answer)
        if failure is not None:
            raise ToolSourceError(f"l'outil « {tool} » a échoué : {failure}")
        if "rows" not in answer:
            raise ToolSourceError(
                f"l'outil « {tool} » ne renvoie pas de table (clé `rows` absente) ; "
                "il doit répondre par une liste de lignes ou {\"rows\": [...]}."
            )
        rows = answer["rows"]
    else:
        rows = answer
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise ToolSourceError(
            f"l'outil « {tool} » ne renvoie pas de données tabulaires "
            "(attendu : liste de dicts, ou {\"rows\": [dicts]})."
        )
    if len(rows) > max_rows:
        raise ToolSourceError(f"l'outil « {tool} » renvoie trop de lignes (plus de {max_rows}).")
    return [{str(k): _flatten(v) for k, v in r.items()} for r in rows]


class ToolQueryExecutor:
    """Runs one allowlisted tool as ``owner_id`` and returns its rows."""

    def __init__(self, owner_id: str, tool_config_id: str | None = None, max_rows: int = 500_000) -> None:
        self._owner = owner_id
        self._config_id = tool_config_id
        self._max_rows = max_rows

    async def run(self, tool: str, args: dict[str, Any]) -> list[dict[str, Any]]:
        if not self._owner:
            raise ToolSourceError("aucun propriétaire pour exécuter l'outil.")
        try:
            fn = resolve_bi_source(tool)
        except BiSourceError as exc:
            raise ToolSourceError(str(exc)) from None
        mode = credentials_of(fn)
        if mode == "tool_config" and not self._config_id:
            raise ToolSourceError(f"l'outil « {tool} » demande une connexion (connection_config_id).")
        try:
            inspect.signature(fn).bind(**args)
        except TypeError as exc:
            raise ToolSourceError(f"arguments invalides pour « {tool} » : {exc}.") from None

        try:
            answer = await asyncio.wait_for(asyncio.to_thread(self._call, fn, tool, args, mode), _TIMEOUT_S)
        except asyncio.TimeoutError:
            raise ToolSourceError(f"l'outil « {tool} » n'a pas répondu en {int(_TIMEOUT_S)} s.") from None
        return rows_from_answer(answer, tool, self._max_rows)

    # -- runs in a worker thread (asyncio.to_thread copies the context, so the
    # -- identity bound below never leaks to the caller) ---------------------

    def _call(self, fn: Callable[..., Any], tool: str, args: dict[str, Any], mode: str) -> Any:
        from apowerb.core.invocation_context import bind_agent_identity, set_current_invoker

        set_current_invoker(self._owner)
        bind_agent_identity(owner=self._owner, organization_id=None, project_id=None, agent_id=None, invocation_id=None)
        if mode != "tool_config":
            return self._invoke(fn, args)
        with _ENV_LOCK:
            return self._invoke_with_config(fn, tool, args)

    @staticmethod
    def _invoke(fn: Callable[..., Any], args: dict[str, Any]) -> Any:
        result = fn(**args)
        if inspect.isawaitable(result):
            async def _wait() -> Any:
                return await result

            return asyncio.run(_wait())
        return result

    def _invoke_with_config(self, fn: Callable[..., Any], tool: str, args: dict[str, Any]) -> Any:
        _, params = load_tool_config_params(str(self._config_id), owner_id=self._owner)
        if not params:
            raise ToolSourceError(f"connexion introuvable ou vide : {self._config_id}.")
        # Variables the tool reads but the owner's config does not set must not
        # fall back to a value another tenant's agent left in the process env.
        with tool_env(params, self._declared_env(fn)):
            return self._invoke(fn, args)

    @staticmethod
    def _declared_env(fn: Callable[..., Any]) -> tuple[str, ...]:
        """Env vars the tool's module reads (best effort; empty if unknown)."""
        category = getattr(fn, "__module__", "").rsplit(".", 1)[-1]
        return declared_env_keys(f"{category}.x")

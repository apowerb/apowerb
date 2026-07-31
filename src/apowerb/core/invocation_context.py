"""Per-invocation context — identity of the user currently running an agent.

Async-safe replacement for relying on the ``AGENT_OWNER`` env var when the
identity that matters is the **invoker** (user currently talking to the
agent), not the **owner** (user who created the agent).

User-personal integrations (Outlook, Gmail, Drive, Calendar, Sheets,
Docs) MUST resolve their tokens against the invoker — otherwise a shared
agent leaks the owner's mailbox/files to anyone who runs it. Agent-shared
resources (BI dashboards scoped by org, Odoo company credentials, MCP
servers configured by the owner) keep using ``AGENT_OWNER``.

Set in /api/adk/run and /api/adk/run_sse handlers; read by user-personal
tools through :func:`resolve_integration_user`.

Implementation note: ``ContextVar`` is async-safe and isolated per
asyncio task, so two invocations served concurrently by the same uvicorn
worker do not race — unlike ``os.environ``.
"""

from __future__ import annotations

import os
from contextvars import ContextVar
from typing import Optional


_current_invoker: ContextVar[Optional[str]] = ContextVar(
    "th2agent_current_invoker", default=None
)


def set_current_invoker(user_email_or_id: Optional[str]) -> None:
    """Bind the invoker for the current async task.

    Call this once at the top of any handler that triggers an agent run,
    after the user has been authenticated. Pass ``None`` to clear.
    """
    _current_invoker.set(user_email_or_id)


def get_current_invoker() -> Optional[str]:
    """Return the invoker set for the current async task, or ``None``."""
    return _current_invoker.get()


def resolve_integration_user(prefer_invoker: bool = True) -> Optional[str]:
    """Pick the user identifier to use when resolving an integration row.

    Args:
        prefer_invoker: When ``True`` (default), return the invoker if it
            is set, falling back to ``AGENT_OWNER`` env var only if no
            invoker is bound (e.g. background scheduler runs). When
            ``False``, always return ``AGENT_OWNER`` — useful for
            agent-shared resources (BI, MCP keys configured by owner).

    Returns:
        The resolved identifier (email or numeric user_id as string), or
        ``None`` if neither source is available.
    """
    if prefer_invoker:
        invoker = _current_invoker.get()
        if invoker:
            return invoker
    return os.getenv("AGENT_OWNER") or None

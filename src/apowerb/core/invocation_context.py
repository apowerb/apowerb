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


# ---------------------------------------------------------------------------
# Owner-scoped identity (owner / organization / project / root agent id).
#
# Historically written to ``os.environ`` by ``to_agent`` at BUILD time and read
# by owner-scoped tools (BI dashboards, S3 buckets, Odoo, MCP) at CALL time.
# With two customers’ agents cached in one uvicorn worker, the second build
# overwrote the first, so the first agent’s tools acted as the second customer
# (cross-customer data exposure, measured 2026-09-29). These are async-safe
# ContextVars, bound per invocation by a ``before_agent_callback`` (see
# ``agent_utils.make_identity_before_agent_callback``), so concurrent runs in
# the same worker never race. Getters fall back to the legacy env var, keeping
# background/scheduler paths that only set the env unchanged.
# ---------------------------------------------------------------------------

_agent_owner_var: ContextVar[Optional[str]] = ContextVar(
    "th2agent_agent_owner", default=None
)
_agent_org_var: ContextVar[Optional[str]] = ContextVar(
    "th2agent_agent_organization_id", default=None
)
_agent_project_var: ContextVar[Optional[str]] = ContextVar(
    "th2agent_agent_project_id", default=None
)
_root_agent_id_var: ContextVar[Optional[str]] = ContextVar(
    "th2agent_root_agent_id", default=None
)
# (invocation_id) for which the root agent id above was recorded. The first
# agent that runs in an invocation is its root; sub-agents must not overwrite
# it, so we only set the root when the invocation changes.
_root_invocation_var: ContextVar[Optional[str]] = ContextVar(
    "th2agent_root_invocation", default=None
)


def bind_agent_identity(
    *,
    owner: Optional[str],
    organization_id: Optional[str],
    project_id: Optional[str],
    agent_id: Optional[str],
    invocation_id: Optional[str],
) -> None:
    """Bind the running agent’s owner-scoped identity for the current task.

    ``owner`` / ``organization_id`` / ``project_id`` are set for every agent
    (including sub-agents) so a tool always sees the identity of the agent that
    invoked it. ``agent_id`` is recorded as the root only for the FIRST agent of
    an invocation — sub-agents keep the root that started the run.
    """
    if owner is not None:
        _agent_owner_var.set(owner)
    if organization_id is not None:
        _agent_org_var.set(organization_id)
    if project_id is not None:
        _agent_project_var.set(project_id)
    if agent_id is not None and _root_invocation_var.get() != invocation_id:
        _root_agent_id_var.set(str(agent_id))
        _root_invocation_var.set(invocation_id)


def get_agent_owner(default: str = "") -> str:
    """Owner email of the running agent (ContextVar, else legacy env var)."""
    val = _agent_owner_var.get()
    if val is not None:
        return val
    return os.getenv("AGENT_OWNER", default)


def get_agent_organization_id(default: str = "default") -> str:
    """Organization id of the running agent (ContextVar, else legacy env var)."""
    val = _agent_org_var.get()
    if val is not None:
        return val
    return os.getenv("AGENT_ORGANIZATION_ID", default)


def get_agent_project_id(default: str = "thaink2") -> str:
    """Project id of the running agent (ContextVar, else legacy env var)."""
    val = _agent_project_var.get()
    if val is not None:
        return val
    return os.getenv("AGENT_PROJECT_ID", default)


def get_root_agent_id(default: str = "") -> str:
    """Root agent id of the current invocation (ContextVar, else env var)."""
    val = _root_agent_id_var.get()
    if val is not None:
        return val
    return os.getenv("ROOT_AGENT_ID", default)


def make_identity_before_agent_callback(
    *,
    owner: Optional[str],
    organization_id: Optional[str],
    project_id: Optional[str],
    agent_id: Optional[str],
):
    """Build a ``before_agent_callback`` that binds this agent identity.

    ADK invokes the callback with the run ``callback_context`` at the start of
    the agent run, inside the same asyncio task that will call the tools, so the
    ContextVars it sets are visible to owner-scoped tools without racing other
    concurrent invocations in the same worker.
    """

    def _bind_identity(callback_context=None):  # noqa: ANN001 - ADK signature
        invocation_id = getattr(callback_context, "invocation_id", None)
        bind_agent_identity(
            owner=owner,
            organization_id=organization_id,
            project_id=project_id,
            agent_id=agent_id,
            invocation_id=invocation_id,
        )
        return None

    return _bind_identity

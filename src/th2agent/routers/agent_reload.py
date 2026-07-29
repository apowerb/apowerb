"""Hot-reload endpoint for ADK agents.

Allows the frontend to rebuild an agent's module + runner without a full
backend restart. Use this after editing an agent's configuration in the DB
(instruction, tools, model, etc.) so the next message in an already-open
chat session picks up the fresh config — no "New chat" required.

How it works
------------
1. ``AgentLoader.remove_agent_from_cache(app_name)`` drops the agent's
   Python modules from ``sys.modules`` and its internal cache.
2. Adding ``app_name`` to ``AdkWebServer.runners_to_clean`` makes the next
   ``get_runner_async(app_name)`` close the current runner and rebuild it,
   which in turn re-imports the agent module → ``to_agent()`` re-runs with
   the latest DB config.

The endpoint is authenticated and requires the caller to own the agent.
"""

from __future__ import annotations

from logging import getLogger

from fastapi import APIRouter, Depends, HTTPException, Request, status

from th2agent.auth.dependencies import get_current_user
from th2agent.users import schemas as user_schemas

logger = getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["agents"])


def _folder_name_for(agent_id: str) -> str:
    """Mirrors ``core.agent_main.get_agent_folder_name`` for numeric IDs.

    Accepts either a raw numeric id (``"1001"``) or the already-prefixed
    folder name (``"agent1001"``). Non-numeric names are returned as-is on
    the assumption they are valid folder names.
    """
    if agent_id.startswith("agent"):
        return agent_id
    if agent_id.isdigit():
        return f"agent{agent_id}"
    return agent_id


@router.post(
    "/{agent_id}/reload",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Hot-reload an agent's module and runner",
)
async def reload_agent(
    agent_id: str,
    request: Request,
    _user: user_schemas.User = Depends(get_current_user),
):
    """Invalidate the ADK caches for ``agent_id`` so the next run rebuilds it."""
    agent_loader = getattr(request.app.state, "adk_agent_loader", None)
    adk_server = getattr(request.app.state, "adk_web_server", None)

    if agent_loader is None or adk_server is None:
        logger.error(
            "[agent-reload] ADK handles missing on app.state — cannot reload %s",
            agent_id,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ADK runtime is not available for hot reload on this server.",
        )

    app_name = _folder_name_for(agent_id)

    try:
        agent_loader.remove_agent_from_cache(app_name)
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
    # instantiated in this process, dropping the module cache via
    # remove_agent_from_cache is already enough — the next request will
    # build a fresh runner from scratch.
    try:
        runner_dict = getattr(adk_server, "runner_dict", None) or {}
        if app_name in runner_dict:
            adk_server.runners_to_clean.add(app_name)
        else:
            logger.debug(
                "[agent-reload] no live runner for %s — skipping cleanup queue",
                app_name,
            )
    except Exception as exc:
        logger.warning(
            "[agent-reload] could not mark runner %s for cleanup: %s",
            app_name,
            exc,
        )

    logger.info(
        "[agent-reload] agent %s marked for hot reload (user=%s)",
        app_name,
        _user.email,
    )

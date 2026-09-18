"""Let a user erase what an agent remembers about them.

``DELETE /api/agents/{agent_id}/memory`` removes the caller's own memory for
that agent, and only theirs: the user is the authenticated one, never a
parameter. The memory scope is ADK's ``(app_name, user_id)``; the chat sends
the user's email as ``user_id`` and ``/api/adk/run`` enforces that it matches
the token, so the email is the key to erase.
"""

from __future__ import annotations

import re
from logging import getLogger

from fastapi import APIRouter, Depends, HTTPException, Response, status

from apowerb.auth.dependencies import get_current_user
from apowerb.memory.service import PersistentMemoryService
from apowerb.routers.agent_reload import _folder_name_for
from apowerb.users import schemas as user_schemas

logger = getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["agents"])

_SAFE_APP_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


def _memory_service() -> PersistentMemoryService:
    from apowerb.configs.settings import get_settings

    return PersistentMemoryService(schema=get_settings().db_schema or None)


@router.delete(
    "/{agent_id}/memory",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Erase what this agent remembers about the current user",
)
async def delete_my_agent_memory(
    agent_id: str,
    current_user: user_schemas.User = Depends(get_current_user),
    service: PersistentMemoryService = Depends(_memory_service),
) -> Response:
    app_name = _folder_name_for(agent_id)
    if not _SAFE_APP_NAME.match(app_name):
        raise HTTPException(status_code=400, detail="invalid agent id")
    deleted = await service.delete_user_memory(app_name=app_name, user_id=current_user.email)
    logger.info("[MEMORY] erased %d entries of %s for %s", deleted, app_name, current_user.email)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

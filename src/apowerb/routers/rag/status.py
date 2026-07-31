"""GET /rag/status/{knowledge_id} and GET /rag/knowledge/{agent_id} endpoints."""

import asyncio
import time
from logging import getLogger
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from apowerb.auth.dependencies import get_current_user
from apowerb.core.knowledge_map import read_knowledge_map, update_status
from apowerb.tools_store.portfolio.rag import tool_get_knowledge
from apowerb.users import schemas as user_schemas

from .validators import _validate_agent_ownership, _validate_session_id

logger = getLogger(__name__)

router = APIRouter()


@router.get("/rag/status/{knowledge_id}", tags=["rag"])
async def get_status(
    knowledge_id: str,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Get the processing status of a specific knowledge base."""
    # TODO(S1a): Ownership validation is not directly possible here because
    # the URL only contains knowledge_id, not agent_id.  A future improvement
    # could look up the knowledge_id in all knowledge maps or add an
    # agent_id query parameter to enforce ownership.

    # S1d — run synchronous tool call in a thread
    result = await asyncio.to_thread(tool_get_knowledge, knowledge_id)

    if result.get("status") == "error":
        raise HTTPException(status_code=404, detail=result.get("message", "Knowledge not found"))

    return {
        "knowledge_id": knowledge_id,
        "is_complete": result.get("is_complete", False),
        "status": "complete" if result.get("is_complete") else "processing",
        "name": result.get("name", ""),
    }


@router.get("/rag/knowledge/{agent_id}", tags=["rag"])
async def get_knowledge_map(
    agent_id: str,
    session_id: Optional[str] = Query(None),
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Read the knowledge map for an agent (or session), refreshing statuses of pending sources."""
    # S1a — ownership check (always against agent_id)
    await _validate_agent_ownership(agent_id, current_user)
    _validate_session_id(session_id)

    kmap = read_knowledge_map(agent_id, session_id=session_id)
    updated = False
    now = time.time()

    for source in kmap.get("sources", []):
        if source.get("status") == "processing":
            kid = source.get("knowledge_id")
            if not kid:
                continue

            # Staleness guard: mark sources stuck in "processing" for > 15 min as error
            created_at = source.get("created_at")
            if created_at:
                try:
                    from datetime import datetime
                    created_ts = datetime.fromisoformat(created_at).timestamp()
                    if now - created_ts > 900:  # 15 minutes
                        source["status"] = "error"
                        update_status(agent_id, kid, "error", session_id=session_id)
                        updated = True
                        logger.warning("[RAG_ROUTER] Stale source kid=%s marked as error (>15min)", kid)
                        continue
                except (ValueError, TypeError):
                    pass

            try:
                # S1d — run synchronous tool call in a thread
                result = await asyncio.to_thread(tool_get_knowledge, kid)
                if result.get("status") == "error":
                    source["status"] = "error"
                    update_status(agent_id, kid, "error", session_id=session_id)
                    updated = True
                elif result.get("is_complete"):
                    source["status"] = "complete"
                    update_status(agent_id, kid, "complete", session_id=session_id)
                    updated = True
            except Exception as exc:
                logger.warning("[RAG_ROUTER] Failed to refresh status for kid=%s: %s", kid, exc)

    return kmap

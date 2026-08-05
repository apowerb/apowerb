"""POST /rag/index-url — download a URL and index it."""

import asyncio
import os
import re
from logging import getLogger

import httpx
from fastapi import APIRouter, Depends, HTTPException

from apowerb.auth.dependencies import get_current_user
from apowerb.configs.settings import get_settings
from apowerb.configs.paths import uploads_dir
from apowerb.tools_store.portfolio.rag import tool_create_knowledge
from apowerb.users import schemas as user_schemas

from .schemas import IndexUrlRequest
from .validators import (
    _validate_agent_ownership,
    _validate_session_id,
    _validate_url_not_internal,
)

logger = getLogger(__name__)

router = APIRouter()

# S1g — bound on manual redirect-following (see _fetch_url_with_ssrf_guard).
MAX_REDIRECTS = 5


async def _fetch_url_with_ssrf_guard(url: str) -> httpx.Response:
    """GET *url*, re-validating every redirect hop against the SSRF guard.

    httpx's built-in ``follow_redirects=True`` does NOT re-run our own
    validation on the ``Location`` header it follows — only the original
    URL was ever checked. An externally-hosted URL that 302s to an internal
    address (metadata service, admin panel, ...) would sail straight
    through. So redirects are followed one hop at a time, here, with
    ``_validate_url_not_internal`` re-applied before every request.
    """
    current_url = url
    async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
        for _ in range(MAX_REDIRECTS + 1):
            _validate_url_not_internal(current_url)
            resp = await client.get(current_url)
            if resp.is_redirect:
                location = resp.headers.get("location")
                if not location:
                    resp.raise_for_status()
                    return resp
                current_url = str(httpx.URL(current_url).join(location))
                continue
            resp.raise_for_status()
            return resp
    raise HTTPException(status_code=400, detail="Too many redirects while downloading URL")


@router.post("/rag/index-url", tags=["rag"])
async def index_url(
    data: IndexUrlRequest,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Download a URL and index its content into a RAG knowledge base."""
    # Resolve patchable symbols via the package for test-time mocking
    from apowerb.routers import rag as _pkg

    # S1a — ownership check (always against agent_id)
    await _validate_agent_ownership(data.agent_id, current_user)
    _validate_session_id(data.session_id)

    # S1g — SSRF protection: reject internal/private URLs before any request
    _validate_url_not_internal(data.url)

    scope = data.session_id or data.agent_id
    upload_dir = str(uploads_dir() / scope)
    os.makedirs(upload_dir, exist_ok=True)

    # Sanitize filename from name or URL. `.` is kept for extensions, but a
    # value made entirely of dots (e.g. "..") must not survive: os.path.join
    # would then point one directory above upload_dir.
    safe_name = re.sub(r"[^a-zA-Z0-9_\-.]", "_", data.name)[:120]
    if not safe_name or safe_name.startswith("."):
        safe_name = "url_download"
    filepath = os.path.join(upload_dir, safe_name)

    # Download the URL (S1g — redirects are re-validated hop by hop)
    try:
        resp = await _fetch_url_with_ssrf_guard(data.url)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("[RAG_ROUTER] Failed to download URL %s: %s", data.url, exc)
        raise HTTPException(status_code=400, detail=f"Failed to download URL: {exc}")

    with open(filepath, "wb") as f:
        f.write(resp.content)

    logger.info("[RAG_ROUTER] Downloaded %s -> %s (%d bytes)", data.url, filepath, len(resp.content))

    # Build callback_url for webhook notifications from th2llm
    settings = get_settings()
    callback_url = f"{settings.public_base_url}/api/rag/webhook"

    # S1d — Index via RAG API in a thread to avoid blocking the event loop
    result = await asyncio.to_thread(
        tool_create_knowledge,
        name=data.name,
        description=f"Document from URL: {data.url}",
        files=[filepath],
        wait_for_completion=False,
        callback_url=callback_url,
    )

    if result.get("status") == "error":
        raise HTTPException(status_code=500, detail=f"Indexation failed: {result.get('message')}")

    knowledge_id = str(result.get("knowledge_id", ""))
    source = _pkg.append_source(
        agent_id=data.agent_id,
        source_type="url",
        name=data.url,
        knowledge_id=knowledge_id,
        status="processing",
        session_id=data.session_id,
    )
    # Register knowledge_id in the streaming manager for webhook → SSE routing
    await _pkg.rag_manager.register_knowledge(knowledge_id, data.agent_id, data.session_id)

    return {"status": "ok", "source": source}

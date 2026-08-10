"""POST /rag/index-files — multipart file upload + indexation."""

import asyncio
import os
import re
from logging import getLogger
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from apowerb.artifacts.upload_mirror import mirror_as_input_artifact
from apowerb.auth.dependencies import get_current_user
from apowerb.configs.settings import get_settings
from apowerb.configs.paths import uploads_dir
from apowerb.tools_store.portfolio.rag import tool_create_knowledge
from apowerb.users import schemas as user_schemas

from .validators import (
    MAX_FILE_SIZE,
    _validate_agent_ownership,
    _validate_session_id,
)

logger = getLogger(__name__)

router = APIRouter()


@router.post("/rag/index-files", tags=["rag"])
async def index_files(
    agent_id: str = Form(...),
    files: List[UploadFile] = File(...),
    session_id: Optional[str] = Form(None),
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Upload and index one or more files into a RAG knowledge base."""
    # Resolve patchable symbols via the package for test-time mocking
    from apowerb.routers import rag as _pkg

    # S1a — ownership check (always against agent_id, session_id is just a sub-scope)
    await _validate_agent_ownership(agent_id, current_user)
    _validate_session_id(session_id)

    # S1e — validate file sizes before processing
    for f in files:
        content_check = await f.read()
        if len(content_check) > MAX_FILE_SIZE:
            raise HTTPException(
                status_code=413,
                detail=f"File {f.filename} exceeds 50MB limit ({len(content_check)} bytes)",
            )
        await f.seek(0)  # reset for later read

    scope = session_id or agent_id
    upload_dir = str(uploads_dir() / scope)
    os.makedirs(upload_dir, exist_ok=True)

    sources = []
    for upload_file in files:
        # S1f — Sanitize filename to prevent path traversal via crafted upload names
        raw_filename = upload_file.filename or "unnamed_file"
        filename = re.sub(r"[^a-zA-Z0-9_\-.]", "_", os.path.basename(raw_filename))[:120]
        if not filename or filename.startswith("."):
            filename = "unnamed_file"
        filepath = os.path.join(upload_dir, filename)

        # Save file to disk
        content = await upload_file.read()
        with open(filepath, "wb") as f:
            f.write(content)

        logger.info("[RAG_ROUTER] Saved %s (%d bytes) for scope %s (agent %s)", filename, len(content), scope, agent_id)

        # Additive mirror into the artifact chain so the upload shows up in
        # the Artifacts tab (kind=input) -- the disk write above is what
        # tool_create_knowledge indexes and is unaffected. Never fails
        # indexation: caught again here even though mirror_as_input_artifact
        # already swallows its own errors, per the "best-effort, log loud"
        # rule this chain has broken silently three times before.
        try:
            await mirror_as_input_artifact(
                app_name=agent_id,
                session_id=session_id,
                filename=filename,
                data=content,
                content_type=upload_file.content_type,
                source="rag",
            )
        except Exception:
            logger.error(
                "[RAG_ROUTER] Artifact mirror raised for %r (agent=%s, session=%s)",
                filename, agent_id, session_id, exc_info=True,
            )

        # Build callback_url for webhook notifications from th2llm
        settings = get_settings()
        callback_url = f"{settings.public_base_url}/api/rag/webhook"

        # S1d — Index via RAG API in a thread to avoid blocking the event loop
        result = await asyncio.to_thread(
            tool_create_knowledge,
            name=filename,
            description=f"Document uploaded: {filename}",
            files=[filepath],
            wait_for_completion=False,
            callback_url=callback_url,
        )

        if result.get("status") == "error":
            logger.error("[RAG_ROUTER] Indexation failed for %s: %s", filename, result.get("message"))
            raise HTTPException(status_code=500, detail=f"Indexation failed for {filename}: {result.get('message')}")

        knowledge_id = str(result.get("knowledge_id", ""))
        source = _pkg.append_source(
            agent_id=agent_id,
            source_type="file",
            name=filename,
            knowledge_id=knowledge_id,
            status="processing",
            session_id=session_id,
        )
        # Register knowledge_id in the streaming manager for webhook → SSE routing
        await _pkg.rag_manager.register_knowledge(knowledge_id, agent_id, session_id)
        sources.append(source)

    return {"status": "ok", "sources": sources}

"""POST /rag/index-s3 — download files from S3 and index them."""

import asyncio
import os
import re
from logging import getLogger

from fastapi import APIRouter, Depends, HTTPException

from apowerb.auth.dependencies import get_current_user
from apowerb.configs.settings import get_settings
from apowerb.configs.paths import uploads_dir
from apowerb.tools_store.portfolio.rag import tool_create_knowledge
from apowerb.tools_store.tools_helpers import set_tool_config_params_as_envvar
from apowerb.users import schemas as user_schemas

from .schemas import IndexS3Request
from .validators import (
    _s3_index_lock,
    _validate_agent_ownership,
    _validate_session_id,
)

logger = getLogger(__name__)

router = APIRouter()


@router.post("/rag/index-s3", tags=["rag"])
async def index_s3(
    data: IndexS3Request,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Download files from S3 and index them into a RAG knowledge base."""
    from apowerb.routers import rag as _pkg

    # S1a — ownership check (always against agent_id)
    await _validate_agent_ownership(data.agent_id, current_user)
    _validate_session_id(data.session_id)

    from apowerb.tools_store.portfolio.s3_tools import _get_s3_client, _parse_s3_url

    scope = data.session_id or data.agent_id
    upload_dir = str(uploads_dir() / scope)
    os.makedirs(upload_dir, exist_ok=True)

    # S1c — Lock to prevent concurrent os.environ mutations for S3 credentials.
    # set_tool_config_params_as_envvar + _get_s3_client must be atomic.
    async with _s3_index_lock:
        set_tool_config_params_as_envvar(
            data.tool_config_id, owner_id=current_user.email
        )

        try:
            s3_client = _get_s3_client()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to create S3 client: {exc}")

        sources = []
        for s3_url in data.s3_urls:
            try:
                bucket, key = _parse_s3_url(s3_url)
            except Exception as exc:
                logger.error("[RAG_ROUTER] Failed to parse S3 URL %s: %s", s3_url, exc)
                raise HTTPException(status_code=400, detail=f"Invalid S3 URL '{s3_url}': {exc}")

            if not bucket or not key:
                raise HTTPException(status_code=400, detail=f"Could not parse bucket/key from S3 URL: {s3_url}")

            # S1f — Sanitize S3 key-derived filename to prevent path traversal
            raw_filename = os.path.basename(key) or "s3_file"
            filename = re.sub(r"[^a-zA-Z0-9_\-.]", "_", raw_filename)[:120]
            if not filename or filename.startswith("."):
                filename = "s3_file"
            filepath = os.path.join(upload_dir, filename)

            # Download from S3 — run synchronous boto3 call in thread (S1d)
            try:
                response = await asyncio.to_thread(s3_client.get_object, Bucket=bucket, Key=key)
                file_bytes = response["Body"].read()
            except Exception as exc:
                logger.error("[RAG_ROUTER] S3 download failed for %s: %s", s3_url, exc)
                raise HTTPException(status_code=500, detail=f"S3 download failed for '{s3_url}': {exc}")

            with open(filepath, "wb") as f:
                f.write(file_bytes)

            logger.info("[RAG_ROUTER] Downloaded s3://%s/%s -> %s (%d bytes)", bucket, key, filepath, len(file_bytes))

            # Build callback_url for webhook notifications from th2llm
            settings = get_settings()
            callback_url = f"{settings.public_base_url}/api/rag/webhook"

            # S1d — Index via RAG API in a thread to avoid blocking the event loop
            result = await asyncio.to_thread(
                tool_create_knowledge,
                name=filename,
                description=f"S3 document: {s3_url}",
                files=[filepath],
                wait_for_completion=False,
                callback_url=callback_url,
            )

            if result.get("status") == "error":
                raise HTTPException(status_code=500, detail=f"Indexation failed for '{s3_url}': {result.get('message')}")

            knowledge_id = str(result.get("knowledge_id", ""))
            source = _pkg.append_source(
                agent_id=data.agent_id,
                source_type="s3",
                name=filename,
                knowledge_id=knowledge_id,
                status="processing",
                session_id=data.session_id,
            )
            # Register knowledge_id in the streaming manager for webhook → SSE routing
            await _pkg.rag_manager.register_knowledge(knowledge_id, data.agent_id, data.session_id)
            sources.append(source)

    return {"status": "ok", "sources": sources}

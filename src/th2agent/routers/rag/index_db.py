"""POST /rag/index-db and /rag/index-db-nl — SQL / natural-language DB indexation.

Includes the synchronous worker helpers ``_sync_index_db`` and
``_sync_index_db_nl`` that encapsulate the blocking boto3 / SQL calls; both are
executed via ``asyncio.to_thread`` under the shared ``_db_index_lock`` to avoid
concurrent ``os.environ`` mutations for DB credentials.
"""

import asyncio
import os
from logging import getLogger

from fastapi import APIRouter, Depends, HTTPException

from th2agent.auth.dependencies import get_current_user
from th2agent.configs.settings import get_settings
from th2agent.tools_store.portfolio.db_to_rag import tool_load_from_db_and_index
from th2agent.tools_store.portfolio.text_to_sql import (
    _generate_sql_with_llm,
    _get_database_schema,
    _validate_sql_safety,
    invalidate_schema_cache,
)
from th2agent.tools_store.tools_helpers import register_tool_config, set_tool_config_params_as_envvar
from th2agent.users import schemas as user_schemas

from .schemas import IndexDbNlRequest, IndexDbRequest
from .validators import (
    _db_index_lock,
    _validate_agent_ownership,
    _validate_session_id,
)

logger = getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# POST /rag/index-db
# ---------------------------------------------------------------------------

@router.post("/rag/index-db", tags=["rag"])
async def index_db(
    data: IndexDbRequest,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Execute a SQL query and index the results into a RAG knowledge base."""
    # Resolve patchable symbols via the package for test-time mocking
    from th2agent.routers import rag as _pkg

    # S1a — ownership check (always against agent_id)
    await _validate_agent_ownership(data.agent_id, current_user)
    _validate_session_id(data.session_id)

    logger.info("[RAG_ROUTER] Indexing DB query for agent %s: %s", data.agent_id, data.sql_query[:200])

    # Build callback_url for webhook notifications from th2llm
    settings = get_settings()
    callback_url = f"{settings.public_base_url}/api/rag/webhook"

    # S1c — Lock to prevent concurrent os.environ mutations for DB credentials
    async with _db_index_lock:
        result = await asyncio.to_thread(
            _pkg._sync_index_db, data, current_user.email, callback_url
        )

    if result.get("status") == "error":
        raise HTTPException(status_code=500, detail=f"DB indexation failed: {result.get('message')}")

    knowledge_id = str(result.get("knowledge_id", ""))
    source = _pkg.append_source(
        agent_id=data.agent_id,
        source_type="db",
        name=data.name,
        knowledge_id=knowledge_id,
        status="processing",
        session_id=data.session_id,
    )
    # Register knowledge_id in the streaming manager for webhook → SSE routing
    await _pkg.rag_manager.register_knowledge(knowledge_id, data.agent_id, data.session_id)

    return {"status": "ok", "source": source}


def _sync_index_db(
    data: IndexDbRequest,
    owner_id: str,
    callback_url: str | None = None,
) -> dict:
    """Synchronous DB indexing — runs in a thread, protected by _db_index_lock."""
    set_tool_config_params_as_envvar(data.tool_config_id, owner_id=owner_id)
    os.environ["AGENT_FOLDER"] = data.session_id or data.agent_id
    return tool_load_from_db_and_index(
        sql_query=data.sql_query,
        knowledge_name=data.name,
        wait_for_completion=False,
        callback_url=callback_url,
    )


# ---------------------------------------------------------------------------
# POST /rag/index-db-nl — Natural language DB indexation
# ---------------------------------------------------------------------------

@router.post("/rag/index-db-nl", tags=["rag"])
async def index_db_nl(
    data: IndexDbNlRequest,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Explore a DB schema, generate SQL from natural language, and index results into RAG."""
    from th2agent.routers import rag as _pkg

    await _validate_agent_ownership(data.agent_id, current_user)
    _validate_session_id(data.session_id)

    # Validation: need either credentials or tool_config_id
    if not data.credentials and not data.tool_config_id:
        raise HTTPException(status_code=400, detail="Either credentials or tool_config_id is required")
    if data.credentials and data.tool_config_id:
        raise HTTPException(status_code=400, detail="Provide either credentials or tool_config_id, not both")

    logger.info("[RAG_ROUTER] NL DB indexation for agent %s: %s", data.agent_id, data.nl_description[:200])

    # Optionally save connector for reuse (non-blocking — don't fail the whole request)
    if data.save_connector and data.credentials:
        try:
            from th2agent.schema.tool_config_schema import ToolConfigCreateSchema
            config_data = ToolConfigCreateSchema(
                tool_name="text_to_sql",
                tool_config_name=data.connector_name or f"DB {data.credentials.database}",
                tool_config_params={
                    "DB_HOST": data.credentials.host,
                    "DB_PORT": str(data.credentials.port),
                    "DB_NAME": data.credentials.database,
                    "DB_USER": data.credentials.user,
                    "DB_PASSWORD": data.credentials.password,
                    "DB_SCHEMA": data.credentials.schema_name,
                },
                tool_category="database",
                owner_id=str(current_user.email),
                organization_id=str(getattr(current_user, "organization_id", "default")),
            )
            register_tool_config(config_data)
        except Exception as exc:
            logger.warning("[RAG_NL] Failed to save connector (non-blocking): %s", exc)

    # Build callback_url for webhook notifications from th2llm
    settings = get_settings()
    callback_url = f"{settings.public_base_url}/api/rag/webhook"

    # Lock to prevent concurrent os.environ mutations for DB credentials
    try:
        async with _db_index_lock:
            result = await asyncio.to_thread(
                _pkg._sync_index_db_nl, data, current_user.email, callback_url
            )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("[RAG_NL] Unhandled error in index_db_nl: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal error during DB indexation: {exc}")

    if result.get("status") in ("error", "partial", "empty"):
        status_code = 422 if result.get("status") == "empty" else 500
        detail = result.get("message", "DB NL indexation failed")
        raise HTTPException(status_code=status_code, detail={
            "message": detail,
            "generated_sql": result.get("generated_sql"),
            "step": result.get("step"),
            "row_count": result.get("row_count"),
            "download_path": result.get("download_path"),
        })

    knowledge_id = str(result.get("knowledge_id", ""))
    if not knowledge_id:
        raise HTTPException(status_code=500, detail="Indexation succeeded but no knowledge_id returned")

    try:
        source = _pkg.append_source(
            agent_id=data.agent_id,
            source_type="db",
            name=data.name,
            knowledge_id=knowledge_id,
            status="processing",
            session_id=data.session_id,
        )
        await _pkg.rag_manager.register_knowledge(knowledge_id, data.agent_id, data.session_id)
    except Exception as exc:
        logger.error("[RAG_NL] Post-indexation registration failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Indexation succeeded (knowledge_id={knowledge_id}) but registration failed: {exc}")

    return {
        "status": "ok",
        "source": source,
        "generated_sql": result.get("generated_sql", ""),
        "row_count": result.get("row_count", 0),
        "columns": result.get("columns", []),
    }


def _sync_index_db_nl(
    data: IndexDbNlRequest,
    owner_id: str,
    callback_url: str | None = None,
) -> dict:
    """Synchronous NL-to-SQL DB indexing — runs in a thread, protected by _db_index_lock."""
    # Set DB credentials in env
    if data.credentials:
        os.environ["DB_HOST"] = data.credentials.host
        os.environ["DB_PORT"] = str(data.credentials.port)
        os.environ["DB_NAME"] = data.credentials.database
        os.environ["DB_USER"] = data.credentials.user
        os.environ["DB_PASSWORD"] = data.credentials.password
        os.environ["DB_SCHEMA"] = data.credentials.schema_name
        # Invalidate schema cache to avoid stale data from another tenant
        invalidate_schema_cache()
    else:
        set_tool_config_params_as_envvar(data.tool_config_id, owner_id=owner_id)

    # Set LLM API key from agent's model config (needed for SQL generation via litellm)
    from th2agent.core.agent_helpers import set_model_params_as_envvar
    set_model_params_as_envvar(data.agent_id)

    os.environ["AGENT_FOLDER"] = data.session_id or data.agent_id

    try:
        # Step 1: Introspect schema
        try:
            schema_info = _get_database_schema()
        except Exception as e:
            logger.error("[RAG_NL] Schema introspection failed: %s", e)
            return {"status": "error", "message": "Database connection failed. Check your credentials.", "step": "schema_discovery"}

        if not schema_info.get("tables"):
            return {"status": "error", "message": "No tables found in the database schema", "step": "schema_discovery"}

        # Step 2: Generate SQL from natural language
        try:
            generated_sql = _generate_sql_with_llm(data.nl_description, schema_info)
        except Exception as e:
            logger.error("[RAG_NL] SQL generation failed: %s", e)
            return {"status": "error", "message": "SQL generation failed. Try rephrasing your request.", "step": "generating_sql"}

        # Step 3: Validate SQL safety
        is_safe, error_msg = _validate_sql_safety(generated_sql)
        if not is_safe:
            return {
                "status": "error",
                "message": f"Generated SQL failed safety validation: {error_msg}",
                "step": "sql_validation",
                "generated_sql": generated_sql,
            }

        # Step 4: Execute SQL and index into RAG
        try:
            result = tool_load_from_db_and_index(
                sql_query=generated_sql,
                knowledge_name=data.name,
                wait_for_completion=False,
                callback_url=callback_url,
            )
        except Exception as e:
            logger.error("[RAG_NL] DB execution/indexation failed: %s", e)
            return {"status": "error", "message": "Query execution failed. The generated SQL may be invalid for your data.", "step": "indexing", "generated_sql": generated_sql}

        result["generated_sql"] = generated_sql
        return result
    finally:
        # CRIT-01: Always purge schema cache after execution to avoid inter-tenant pollution
        invalidate_schema_cache()

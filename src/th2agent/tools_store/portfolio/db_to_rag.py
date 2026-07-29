"""DB → RAG tool — Execute a SQL query, export results to CSV, and index into a RAG knowledge base.

Combines database connectivity (psycopg2) with RAG indexing (Thaink2 API) so an
agent can load structured data from a database and make it queryable via semantic
search with ``tool_search_knowledge``.

Environment variables (via tool_config):
  - DB: ``DB_HOST``, ``DB_PORT``, ``DB_NAME``, ``DB_USER``, ``DB_PASSWORD``, ``DB_SCHEMA``
  - RAG: ``RAG_BASE_URL``, ``th2username``, ``th2password``
"""

import csv
import os
import re
from datetime import date, datetime
from decimal import Decimal
from logging import getLogger
from pathlib import Path

import psycopg2

from th2agent.configs.paths import uploads_dir
from psycopg2.extras import RealDictCursor

from th2agent.tools_store.portfolio.rag import tool_create_knowledge

logger = getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _json_safe(obj):
    """Convert non-JSON-serializable types to strings (same logic as text_to_sql)."""
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj) if obj.is_finite() else str(obj)
    if isinstance(obj, bytes):
        return "<binary>"
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def _get_connection():
    """Open a psycopg2 connection using DB_* env vars (same pattern as database.py)."""
    host = os.getenv("DB_HOST", "localhost")
    port = int(os.getenv("DB_PORT", "5432"))
    database = os.getenv("DB_NAME")
    user = os.getenv("DB_USER")
    password = os.getenv("DB_PASSWORD")

    if not all([database, user, password]):
        raise ValueError("Missing required database credentials (DB_NAME, DB_USER, DB_PASSWORD)")

    return psycopg2.connect(
        host=host, port=port, database=database, user=user, password=password,
    )


def _validate_sql(sql: str) -> tuple[bool, str | None]:
    """Validate that the query is a safe SELECT statement."""
    sql_upper = sql.strip().upper()

    if not sql_upper.startswith("SELECT"):
        return False, "Only SELECT queries are allowed."

    # Reject multi-statement queries
    if ";" in sql.strip().rstrip(";"):
        return False, "Multi-statement queries are not allowed."

    dangerous = [
        "INSERT", "UPDATE", "DELETE", "DROP", "TRUNCATE",
        "ALTER", "CREATE", "GRANT", "REVOKE", "EXEC", "EXECUTE",
    ]
    for keyword in dangerous:
        if re.search(rf"\b{keyword}\b", sql_upper):
            return False, f"Query contains forbidden keyword: {keyword}"

    return True, None


def _ensure_limit(sql: str, max_rows: int) -> str:
    """Append a LIMIT clause if none is present."""
    if "LIMIT" not in sql.upper():
        return f"{sql.rstrip(';')} LIMIT {max_rows}"
    return sql


# ---------------------------------------------------------------------------
# Public tool (auto-discovered via the ``tool_`` prefix)
# ---------------------------------------------------------------------------

def tool_load_from_db_and_index(
    sql_query: str,
    knowledge_name: str,
    description: str = "",
    max_rows: int = 500,
    prompt: str = "You are a helpful assistant that answers questions about the provided data.",
    wait_for_completion: bool = True,
    callback_url: str | None = None,
) -> dict:
    """Execute a SQL SELECT query on the configured database, export the results
    as a CSV file, and index it into a Thaink2 RAG knowledge base.

    The CSV file is always available for download even if RAG indexing fails.
    After successful indexing, use ``tool_search_knowledge(knowledge_id=<returned id>, query=...)``
    to run semantic queries on the data.

    Args:
        sql_query: A valid PostgreSQL SELECT query to execute.
        knowledge_name: Human-readable name for the knowledge base.
        description: Description of the data being indexed.
        max_rows: Maximum number of rows to export (default 500).
        prompt: System prompt for the RAG engine (optional).
        wait_for_completion: If True, poll until indexing finishes (default True).

    Environment Variables:
        DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD, DB_SCHEMA
        RAG_BASE_URL, th2username, th2password

    Returns:
        dict with keys: status, knowledge_id, sql_query, row_count, columns,
        csv_path, download_path, message.
    """
    # 1. Validate SQL safety
    is_safe, error_msg = _validate_sql(sql_query)
    if not is_safe:
        return {"status": "error", "message": f"SQL validation failed: {error_msg}"}

    # 2. Execute the query
    try:
        db_schema = os.getenv("DB_SCHEMA", "public")
        safe_query = _ensure_limit(sql_query.strip(), max_rows)

        conn = _get_connection()
        try:
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            cursor.execute(f'SET search_path TO "{db_schema}", public')
            cursor.execute(safe_query)
            rows = cursor.fetchall()
            columns = [desc[0] for desc in cursor.description] if cursor.description else []
            cursor.close()
        finally:
            conn.close()

        if not rows:
            return {
                "status": "empty",
                "message": "Query returned 0 rows — nothing to index.",
                "sql_query": safe_query,
                "row_count": 0,
            }

        serialized = [_json_safe(dict(row)) for row in rows]

    except ValueError as e:
        return {"status": "error", "message": str(e)}
    except psycopg2.OperationalError as e:
        return {"status": "error", "message": f"Database connection error: {e}"}
    except psycopg2.ProgrammingError as e:
        return {"status": "error", "message": f"SQL error: {e}"}
    except Exception as e:
        return {"status": "error", "message": f"Unexpected DB error: {e}"}

    # 3. Write CSV to uploads/{AGENT_FOLDER}/
    try:
        folder = os.getenv("AGENT_FOLDER", "default")
        out_dir = uploads_dir() / folder
        out_dir.mkdir(parents=True, exist_ok=True)

        safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", knowledge_name)[:80]
        csv_filename = f"db_export_{safe_name}.csv"
        csv_path = str(out_dir / csv_filename)
        download_path = f"/api/files/{folder}/{csv_filename}"

        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=columns)
            writer.writeheader()
            writer.writerows(serialized)

    except Exception as e:
        return {"status": "error", "message": f"Failed to write CSV: {e}"}

    # 4. Index via RAG API (reuses tool_create_knowledge from rag.py)
    logger.info(
        "[DB_TO_RAG] CSV ready (%d rows, %s). Starting RAG indexation for '%s'...",
        len(serialized), csv_path, knowledge_name,
    )
    rag_result = tool_create_knowledge(
        name=knowledge_name,
        description=description or f"Data exported from SQL query: {sql_query[:200]}",
        files=[csv_path],
        prompt=prompt,
        wait_for_completion=wait_for_completion,
        callback_url=callback_url,
    )

    knowledge_id = rag_result.get("knowledge_id")
    rag_status = rag_result.get("status", "error")

    logger.info(
        "[DB_TO_RAG] RAG result: status=%s, knowledge_id=%s, message=%s",
        rag_status, knowledge_id, rag_result.get("message", ""),
    )

    if rag_status == "error":
        rag_error_detail = rag_result.get("message", "unknown error")
        logger.error(
            "[DB_TO_RAG] RAG indexing failed for '%s'. Full RAG error: %s",
            knowledge_name, rag_error_detail,
        )
        return {
            "status": "partial",
            "message": (
                f"DB export succeeded ({len(serialized)} rows) but RAG indexing failed: "
                f"{rag_error_detail}. "
                f"The CSV file is still available for download."
            ),
            "rag_error_detail": rag_error_detail,
            "sql_query": safe_query,
            "row_count": len(serialized),
            "columns": columns,
            "csv_path": csv_path,
            "download_path": download_path,
        }

    return {
        "status": rag_status,
        "knowledge_id": knowledge_id,
        "sql_query": safe_query,
        "row_count": len(serialized),
        "columns": columns,
        "csv_path": csv_path,
        "download_path": download_path,
        "message": (
            f"Indexed {len(serialized)} rows into knowledge_id={knowledge_id}. "
            f"Use tool_search_knowledge(knowledge_id={knowledge_id}, query=...) to query. "
            f"CSV also available for download."
        ),
    }

"""Oracle Database tools — run read-only SQL and list tables.

Uses python-oracledb in its default *thin* mode (pure Python, no Oracle
Client libraries), checked against oracledb 26.0.1 on 2026-10-06:
``oracledb.connect(user=, password=, dsn=)`` with an Easy Connect DSN
(``host:port/service_name``), ``Connection.call_timeout`` (ms),
``Cursor.description`` / ``fetchmany``.

Read-only twice over: the shared guard refuses anything but a single
SELECT / WITH statement, and every call runs inside
``SET TRANSACTION READ ONLY`` then rolls back. The configured user should
still only hold SELECT grants.

``ORACLE_*`` settings are read at module level so the ToolsStore parameter
scanner surfaces them in the UI, and again at call time.
"""

from __future__ import annotations

import datetime
import decimal
import os
import re
from logging import getLogger
from typing import Any

from apowerb.tools_store.portfolio.warehouse_core import (
    clamp_rows,
    rows_result,
    validate_read_only_sql,
)

logger = getLogger(__name__)

# Read at module level so the ToolsStore scanner (regex on os.getenv) discovers
# them for the UI. Re-read at call time.
_ORACLE_DSN = os.getenv("ORACLE_DSN", "")
_ORACLE_USER = os.getenv("ORACLE_USER", "")
_ORACLE_PASSWORD = os.getenv("ORACLE_PASSWORD", "")

_CALL_TIMEOUT_MS = 60_000
_MAX_LOB_CHARS = 10_000
_OWNER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]*$")


def _env(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise EnvironmentError(f"{name} not set")
    return value


def _connect():
    import oracledb

    conn = oracledb.connect(
        user=_env("ORACLE_USER"),
        password=_env("ORACLE_PASSWORD"),
        dsn=_env("ORACLE_DSN"),
    )
    conn.call_timeout = _CALL_TIMEOUT_MS
    return conn


def _value(value: Any) -> Any:
    """Make a fetched value JSON-friendly."""
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, bytes):
        return "<binary>"
    if hasattr(value, "read"):  # CLOB / NCLOB / BLOB
        data = value.read(1, _MAX_LOB_CHARS)
        return data if isinstance(data, str) else "<binary>"
    return value


def _run(statement: str, max_rows: int, params: dict | None = None) -> dict:
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SET TRANSACTION READ ONLY")
        cursor.execute(statement, params or {})
        columns = [col[0] for col in cursor.description or []]
        # One extra row tells whether the result was cut.
        rows = [[_value(v) for v in row] for row in cursor.fetchmany(max_rows + 1)]
        conn.rollback()
        return rows_result(columns, rows, max_rows, None)
    finally:
        conn.close()


def _error_result(exc: Exception) -> dict:
    if isinstance(exc, (EnvironmentError, ValueError)):
        return {"status": "error", "error_message": str(exc)}
    logger.warning("[ORACLE] query failed: %s", exc)
    return {"status": "error", "error_message": f"Oracle error: {exc}"}


def tool_oracle_run_query(sql: str, max_rows: int = 100) -> dict:
    """Run a read-only SQL query on Oracle Database.

    Only a single SELECT / WITH statement is accepted (no trailing semicolon
    needed). Use ``FETCH FIRST n ROWS ONLY`` to limit on the server side.

    Args:
        sql (str): The query (Oracle SQL). Required.
        max_rows (int): Maximum rows to return (1–1000). Default: 100.

    Returns:
        dict: On success, ``status`` "success" with ``columns``, ``rows`` (list
        of dicts), ``row_count`` and ``truncated``. On failure, ``status``
        "error" with ``error_message``.
    """
    try:
        statement = validate_read_only_sql(sql)
        return _run(statement, clamp_rows(max_rows))
    except Exception as exc:  # noqa: BLE001 — mapped to a structured error dict
        return _error_result(exc)


def tool_oracle_list_tables(owner: str | None = None, max_rows: int = 500) -> dict:
    """List the tables and views visible to the configured Oracle user.

    Args:
        owner (str): Restrict to this schema owner (e.g. "SALES"). Optional —
            otherwise the user's own objects.
        max_rows (int): Maximum tables to return (1–1000). Default: 500.

    Returns:
        dict: Same shape as ``tool_oracle_run_query``, one row per table or
        view (``OWNER``, ``NAME``, ``TYPE``).
    """
    if owner and not _OWNER_RE.match(owner.strip()):
        return {"status": "error", "error_message": "Invalid `owner` name."}
    if owner:
        sql = (
            "SELECT owner, table_name AS name, 'TABLE' AS type FROM all_tables "
            "WHERE owner = :owner UNION ALL "
            "SELECT owner, view_name, 'VIEW' FROM all_views WHERE owner = :owner "
            "ORDER BY 1, 2"
        )
        params = {"owner": owner.strip().upper()}
    else:
        sql = (
            "SELECT USER AS owner, table_name AS name, 'TABLE' AS type FROM user_tables "
            "UNION ALL SELECT USER, view_name, 'VIEW' FROM user_views ORDER BY 1, 2"
        )
        params = {}
    try:
        return _run(sql, clamp_rows(max_rows), params)
    except Exception as exc:  # noqa: BLE001 — mapped to a structured error dict
        return _error_result(exc)

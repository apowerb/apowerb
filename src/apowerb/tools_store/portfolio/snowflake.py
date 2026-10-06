"""Snowflake tools — run read-only SQL and list tables through the SQL API.

API contract
------------
Verified against the Snowflake SQL API docs (docs.snowflake.com), consulted
2026-10-06:

* Endpoint: ``POST https://<account>.snowflakecomputing.com/api/v2/statements``
  with ``statement``, ``timeout``, ``warehouse``, ``database``, ``schema``,
  ``role``.
* Auth: a programmatic access token (PAT) in ``Authorization: Bearer <token>``
  with ``X-Snowflake-Authorization-Token-Type: PROGRAMMATIC_ACCESS_TOKEN``. By
  default the PAT's user must be subject to a network policy.
* 200 → result set: ``resultSetMetaData.rowType[].name``, ``numRows`` and
  ``data`` (array of arrays, every value a string or null). 202 → still
  running: poll ``GET /api/v2/statements/{statementHandle}``. 422 → failure.
* Only the first partition is read: results are capped well below its size.

No driver is used (plain HTTPS through httpx), so nothing heavy is loaded in
the application pod. Queries go through the shared read-only guard.

``SNOWFLAKE_*`` settings are read at module level so the ToolsStore parameter
scanner surfaces them in the UI, and again at call time.
"""

from __future__ import annotations

import os
import re
import time
from logging import getLogger
from typing import Any

from apowerb.tools_store.portfolio.warehouse_core import (
    POLL_BUDGET_S,
    POLL_INTERVAL_S,
    clamp_rows,
    error_result,
    rows_result,
    validate_read_only_sql,
)

logger = getLogger(__name__)

# Read at module level so the ToolsStore scanner (regex on os.getenv) discovers
# them for the UI. Re-read at call time in _config().
_SNOWFLAKE_ACCOUNT = os.getenv("SNOWFLAKE_ACCOUNT", "")
_SNOWFLAKE_TOKEN = os.getenv("SNOWFLAKE_TOKEN", "")
_SNOWFLAKE_WAREHOUSE = os.getenv("SNOWFLAKE_WAREHOUSE", "")
_SNOWFLAKE_DATABASE = os.getenv("SNOWFLAKE_DATABASE", "")
_SNOWFLAKE_SCHEMA = os.getenv("SNOWFLAKE_SCHEMA", "")
_SNOWFLAKE_ROLE = os.getenv("SNOWFLAKE_ROLE", "")

_HTTP_TIMEOUT_S = 60
_STATEMENT_TIMEOUT_S = 60
_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _account() -> str:
    """Return the account identifier, reduced from a full URL if needed."""
    raw = (os.environ.get("SNOWFLAKE_ACCOUNT") or "").strip()
    if not raw:
        raise EnvironmentError("SNOWFLAKE_ACCOUNT not set")
    raw = re.sub(r"^https?://", "", raw, flags=re.I).split("/")[0]
    raw = re.sub(r"\.snowflakecomputing\.com$", "", raw, flags=re.I)
    if not _ACCOUNT_RE.match(raw):
        raise EnvironmentError(f"SNOWFLAKE_ACCOUNT is not a valid account: {raw!r}")
    return raw


def _token() -> str:
    token = os.environ.get("SNOWFLAKE_TOKEN") or ""
    if not token:
        raise EnvironmentError("SNOWFLAKE_TOKEN not set")
    return token


def _request(method: str, url: str, *, json_body: dict | None = None) -> Any:
    import httpx

    resp = httpx.request(
        method,
        url,
        headers={
            "Authorization": f"Bearer {_token()}",
            "X-Snowflake-Authorization-Token-Type": "PROGRAMMATIC_ACCESS_TOKEN",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "apowerb",
        },
        json=json_body,
        timeout=_HTTP_TIMEOUT_S,
    )
    resp.raise_for_status()
    return resp


def _execute(statement: str) -> dict:
    """Submit a statement and wait for its first result partition."""
    base = f"https://{_account()}.snowflakecomputing.com/api/v2/statements"
    body: dict[str, Any] = {"statement": statement, "timeout": _STATEMENT_TIMEOUT_S}
    for key in ("warehouse", "database", "schema", "role"):
        value = os.environ.get(f"SNOWFLAKE_{key.upper()}")
        if value:
            body[key] = value

    resp = _request("POST", base, json_body=body)
    deadline = time.monotonic() + POLL_BUDGET_S
    while resp.status_code == 202:
        handle = resp.json().get("statementHandle")
        if not handle:
            raise RuntimeError("Snowflake returned 202 without a statementHandle.")
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"Snowflake statement {handle} still running after {POLL_BUDGET_S}s."
            )
        time.sleep(POLL_INTERVAL_S)
        resp = _request("GET", f"{base}/{handle}")
    return resp.json()


def _to_rows(data: dict, max_rows: int) -> dict:
    meta = data.get("resultSetMetaData") or {}
    columns = [col.get("name") for col in meta.get("rowType") or []]
    return rows_result(columns, data.get("data") or [], max_rows, meta.get("numRows"))


def tool_snowflake_run_query(sql: str, max_rows: int = 100) -> dict:
    """Run a read-only SQL query on Snowflake.

    Only single SELECT / WITH / SHOW / DESCRIBE / EXPLAIN statements are
    accepted. Values come back as strings (Snowflake SQL API format).

    Args:
        sql (str): The query. Required.
        max_rows (int): Maximum rows to return (1–1000). Default: 100.

    Returns:
        dict: On success, ``status`` "success" with ``columns``, ``rows`` (list
        of dicts), ``row_count``, ``total_rows`` and ``truncated``. On failure,
        ``status`` "error" with ``error_message``.
    """
    try:
        statement = validate_read_only_sql(sql)
        data = _execute(statement)
    except Exception as exc:  # noqa: BLE001 — mapped to a structured error dict
        return error_result(exc, "Snowflake", "SNOWFLAKE_TOKEN", logger)
    return _to_rows(data, clamp_rows(max_rows))


def tool_snowflake_list_tables(schema: str | None = None, max_rows: int = 500) -> dict:
    """List the tables and views of the configured Snowflake database.

    Args:
        schema (str): Restrict to this schema. Optional.
        max_rows (int): Maximum tables to return (1–1000). Default: 500.

    Returns:
        dict: Same shape as ``tool_snowflake_run_query``, one row per table
        (``TABLE_SCHEMA``, ``TABLE_NAME``, ``TABLE_TYPE``, ``ROW_COUNT``).
    """
    sql = (
        "SELECT table_schema, table_name, table_type, row_count "
        "FROM information_schema.tables "
        "WHERE table_schema <> 'INFORMATION_SCHEMA'"
    )
    if schema and schema.strip():
        if not re.match(r"^[A-Za-z0-9_$]+$", schema.strip()):
            return {"status": "error", "error_message": "Invalid `schema` name."}
        sql += f" AND table_schema = '{schema.strip().upper()}'"
    sql += " ORDER BY table_schema, table_name"
    try:
        data = _execute(sql)
    except Exception as exc:  # noqa: BLE001 — mapped to a structured error dict
        return error_result(exc, "Snowflake", "SNOWFLAKE_TOKEN", logger)
    return _to_rows(data, clamp_rows(max_rows))

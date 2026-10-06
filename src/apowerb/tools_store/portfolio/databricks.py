"""Databricks tools — run read-only SQL and list tables on a SQL warehouse.

API contract
------------
Verified against the Databricks REST reference (docs.databricks.com,
Statement Execution API), consulted 2026-10-06:

* ``POST https://<workspace-host>/api/2.0/sql/statements`` with
  ``warehouse_id``, ``statement``, ``wait_timeout`` (``"0s"`` or 5–50 s),
  ``on_wait_timeout`` (``CONTINUE``/``CANCEL``), ``disposition`` ``INLINE``,
  ``format`` ``JSON_ARRAY``, ``row_limit``, ``catalog``, ``schema``.
* Auth: a personal access token (or OAuth token) in
  ``Authorization: Bearer <token>``.
* Response: ``statement_id``, ``status.state`` (PENDING, RUNNING, SUCCEEDED,
  FAILED, CANCELED, CLOSED), ``manifest.schema.columns[].name``,
  ``manifest.total_row_count``, ``result.data_array``. While pending, poll
  ``GET /api/2.0/sql/statements/{statement_id}``.

No driver is used (plain HTTPS through httpx), so nothing heavy is loaded in
the application pod. Queries go through the shared read-only guard.

``DATABRICKS_*`` settings are read at module level so the ToolsStore parameter
scanner surfaces them in the UI, and again at call time.
"""

from __future__ import annotations

import os
import re
import time
from logging import getLogger
from typing import Any
from urllib.parse import urlsplit

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
# them for the UI. Re-read at call time.
_DATABRICKS_HOST = os.getenv("DATABRICKS_HOST", "")
_DATABRICKS_TOKEN = os.getenv("DATABRICKS_TOKEN", "")
_DATABRICKS_WAREHOUSE_ID = os.getenv("DATABRICKS_WAREHOUSE_ID", "")
_DATABRICKS_CATALOG = os.getenv("DATABRICKS_CATALOG", "")
_DATABRICKS_SCHEMA = os.getenv("DATABRICKS_SCHEMA", "")

_HTTP_TIMEOUT_S = 60
_PENDING = ("PENDING", "RUNNING")
_HOST_RE = re.compile(r"^[A-Za-z0-9.-]+$")


def _env(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise EnvironmentError(f"{name} not set")
    return value


def _base_url() -> str:
    """Return ``https://<host>`` from DATABRICKS_HOST (URL or bare host)."""
    raw = _env("DATABRICKS_HOST")
    host = urlsplit(raw if "://" in raw else f"https://{raw}").hostname or ""
    if not _HOST_RE.match(host):
        raise EnvironmentError(f"DATABRICKS_HOST is not a valid host: {raw!r}")
    return f"https://{host}"


def _request(method: str, path: str, *, json_body: dict | None = None) -> dict:
    import httpx

    resp = httpx.request(
        method,
        f"{_base_url()}{path}",
        headers={
            "Authorization": f"Bearer {_env('DATABRICKS_TOKEN')}",
            "Content-Type": "application/json",
        },
        json=json_body,
        timeout=_HTTP_TIMEOUT_S,
    )
    resp.raise_for_status()
    return resp.json()


def _execute(statement: str, max_rows: int) -> dict:
    body: dict[str, Any] = {
        "warehouse_id": _env("DATABRICKS_WAREHOUSE_ID"),
        "statement": statement,
        "wait_timeout": "30s",
        "on_wait_timeout": "CONTINUE",
        "disposition": "INLINE",
        "format": "JSON_ARRAY",
        # One extra row tells us whether the result was cut.
        "row_limit": max_rows + 1,
    }
    for key in ("catalog", "schema"):
        value = os.environ.get(f"DATABRICKS_{key.upper()}")
        if value:
            body[key] = value

    data = _request("POST", "/api/2.0/sql/statements", json_body=body)
    deadline = time.monotonic() + POLL_BUDGET_S
    while (data.get("status") or {}).get("state") in _PENDING:
        statement_id = data.get("statement_id")
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"Databricks statement {statement_id} still running after "
                f"{POLL_BUDGET_S}s."
            )
        time.sleep(POLL_INTERVAL_S)
        data = _request("GET", f"/api/2.0/sql/statements/{statement_id}")

    status = data.get("status") or {}
    if status.get("state") != "SUCCEEDED":
        message = (status.get("error") or {}).get("message") or ""
        raise ValueError(
            f"Databricks statement {status.get('state')}: {message}".strip()
        )
    return data


def _to_rows(data: dict, max_rows: int) -> dict:
    manifest = data.get("manifest") or {}
    columns = [
        c.get("name") for c in (manifest.get("schema") or {}).get("columns") or []
    ]
    rows = (data.get("result") or {}).get("data_array") or []
    total = manifest.get("total_row_count")
    if manifest.get("truncated") and total is not None and total <= len(rows):
        total = None  # total of the truncated result, not of the query
    return rows_result(columns, rows, max_rows, total)


def tool_databricks_run_query(sql: str, max_rows: int = 100) -> dict:
    """Run a read-only SQL query on a Databricks SQL warehouse.

    Only single SELECT / WITH / SHOW / DESCRIBE / EXPLAIN statements are
    accepted.

    Args:
        sql (str): The query. Required.
        max_rows (int): Maximum rows to return (1–1000). Default: 100.

    Returns:
        dict: On success, ``status`` "success" with ``columns``, ``rows`` (list
        of dicts), ``row_count``, ``total_rows`` and ``truncated``. On failure,
        ``status`` "error" with ``error_message``.
    """
    cap = clamp_rows(max_rows)
    try:
        statement = validate_read_only_sql(sql)
        data = _execute(statement, cap)
    except Exception as exc:  # noqa: BLE001 — mapped to a structured error dict
        return error_result(exc, "Databricks", "DATABRICKS_TOKEN", logger)
    return _to_rows(data, cap)


def tool_databricks_list_tables(
    catalog: str | None = None, schema: str | None = None, max_rows: int = 500
) -> dict:
    """List the tables of a Databricks catalog (Unity Catalog).

    Args:
        catalog (str): Catalog to list. Defaults to DATABRICKS_CATALOG, or the
            warehouse's current catalog.
        schema (str): Restrict to this schema. Optional.
        max_rows (int): Maximum tables to return (1–1000). Default: 500.

    Returns:
        dict: Same shape as ``tool_databricks_run_query``, one row per table
        (``table_catalog``, ``table_schema``, ``table_name``, ``table_type``).
    """
    name_re = re.compile(r"^[A-Za-z0-9_]+$")
    catalog = (catalog or os.environ.get("DATABRICKS_CATALOG") or "").strip()
    schema = (schema or "").strip()
    if (catalog and not name_re.match(catalog)) or (
        schema and not name_re.match(schema)
    ):
        return {
            "status": "error",
            "error_message": "Invalid `catalog` or `schema` name.",
        }
    source = (
        f"`{catalog}`.information_schema.tables"
        if catalog
        else "information_schema.tables"
    )
    sql = (
        "SELECT table_catalog, table_schema, table_name, table_type "
        f"FROM {source} WHERE table_schema <> 'information_schema'"
    )
    if schema:
        sql += f" AND table_schema = '{schema}'"
    sql += " ORDER BY table_schema, table_name"
    cap = clamp_rows(max_rows)
    try:
        data = _execute(sql, cap)
    except Exception as exc:  # noqa: BLE001 — mapped to a structured error dict
        return error_result(exc, "Databricks", "DATABRICKS_TOKEN", logger)
    return _to_rows(data, cap)

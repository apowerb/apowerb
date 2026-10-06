"""Amazon Redshift tools — run read-only SQL and list tables via the Data API.

API contract
------------
Verified against the ``redshift-data`` service model shipped with boto3
(already a dependency), read 2026-10-06:

* ``ExecuteStatement(Sql, Database, ClusterIdentifier | WorkgroupName,
  DbUser | SecretArn)`` → ``Id``.
* ``DescribeStatement(Id)`` → ``Status`` (SUBMITTED, PICKED, STARTED, FINISHED,
  ABORTED, FAILED), ``Error``, ``HasResultSet``.
* ``GetStatementResult(Id)`` → ``ColumnMetadata[].name``, ``Records`` (cells
  ``{stringValue|longValue|doubleValue|booleanValue|isNull|blobValue}``),
  ``TotalNumRows``.

The Data API is HTTPS + IAM: no VPC access to the cluster is needed and no
connection is held open. Provisioned clusters use ``REDSHIFT_CLUSTER_ID``,
Serverless uses ``REDSHIFT_WORKGROUP``. Queries go through the shared
read-only guard.

``REDSHIFT_*`` settings are read at module level so the ToolsStore parameter
scanner surfaces them in the UI, and again at call time. Without
``REDSHIFT_ACCESS_KEY_ID`` boto3's default credential chain is used.
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
# them for the UI. Re-read at call time.
_REDSHIFT_REGION = os.getenv("REDSHIFT_REGION", "")
_REDSHIFT_ACCESS_KEY_ID = os.getenv("REDSHIFT_ACCESS_KEY_ID", "")
_REDSHIFT_SECRET_ACCESS_KEY = os.getenv("REDSHIFT_SECRET_ACCESS_KEY", "")
_REDSHIFT_DATABASE = os.getenv("REDSHIFT_DATABASE", "")
_REDSHIFT_CLUSTER_ID = os.getenv("REDSHIFT_CLUSTER_ID", "")
_REDSHIFT_WORKGROUP = os.getenv("REDSHIFT_WORKGROUP", "")
_REDSHIFT_DB_USER = os.getenv("REDSHIFT_DB_USER", "")
_REDSHIFT_SECRET_ARN = os.getenv("REDSHIFT_SECRET_ARN", "")

_FAILED = ("ABORTED", "FAILED")


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _client():
    import boto3

    region = _env("REDSHIFT_REGION")
    if not region:
        raise EnvironmentError("REDSHIFT_REGION not set")
    kwargs: dict[str, Any] = {"region_name": region}
    if _env("REDSHIFT_ACCESS_KEY_ID"):
        kwargs["aws_access_key_id"] = _env("REDSHIFT_ACCESS_KEY_ID")
        kwargs["aws_secret_access_key"] = _env("REDSHIFT_SECRET_ACCESS_KEY")
    return boto3.client("redshift-data", **kwargs)


def _target() -> dict:
    """ExecuteStatement arguments naming the database and how to log in."""
    database = _env("REDSHIFT_DATABASE")
    if not database:
        raise EnvironmentError("REDSHIFT_DATABASE not set")
    target: dict[str, Any] = {"Database": database}
    if _env("REDSHIFT_WORKGROUP"):
        target["WorkgroupName"] = _env("REDSHIFT_WORKGROUP")
    elif _env("REDSHIFT_CLUSTER_ID"):
        target["ClusterIdentifier"] = _env("REDSHIFT_CLUSTER_ID")
    else:
        raise EnvironmentError("Set REDSHIFT_CLUSTER_ID or REDSHIFT_WORKGROUP")
    if _env("REDSHIFT_SECRET_ARN"):
        target["SecretArn"] = _env("REDSHIFT_SECRET_ARN")
    elif _env("REDSHIFT_DB_USER"):
        target["DbUser"] = _env("REDSHIFT_DB_USER")
    elif "ClusterIdentifier" in target:
        raise EnvironmentError("Set REDSHIFT_SECRET_ARN or REDSHIFT_DB_USER")
    return target


def _cell(field: dict) -> Any:
    if field.get("isNull"):
        return None
    for key in ("stringValue", "longValue", "doubleValue", "booleanValue"):
        if key in field:
            return field[key]
    if "blobValue" in field:
        return "<binary>"
    return None


def _execute(statement: str, max_rows: int) -> dict:
    client = _client()
    started = client.execute_statement(Sql=statement, **_target())
    statement_id = started["Id"]
    deadline = time.monotonic() + POLL_BUDGET_S
    while True:
        desc = client.describe_statement(Id=statement_id)
        status = desc.get("Status")
        if status == "FINISHED":
            break
        if status in _FAILED:
            raise ValueError(
                f"Redshift statement {status}: {desc.get('Error', '')}".strip()
            )
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"Redshift statement {statement_id} still running after {POLL_BUDGET_S}s."
            )
        time.sleep(POLL_INTERVAL_S)

    if not desc.get("HasResultSet"):
        return rows_result([], [], max_rows, 0)
    result = client.get_statement_result(Id=statement_id)
    columns = [col.get("name") for col in result.get("ColumnMetadata") or []]
    rows = [[_cell(f) for f in record] for record in result.get("Records") or []]
    return rows_result(columns, rows, max_rows, result.get("TotalNumRows"))


def tool_redshift_run_query(sql: str, max_rows: int = 100) -> dict:
    """Run a read-only SQL query on Amazon Redshift.

    Only single SELECT / WITH / SHOW / DESCRIBE / EXPLAIN statements are
    accepted.

    Args:
        sql (str): The query (Redshift SQL). Required.
        max_rows (int): Maximum rows to return (1–1000). Default: 100.

    Returns:
        dict: On success, ``status`` "success" with ``columns``, ``rows`` (list
        of dicts), ``row_count``, ``total_rows`` and ``truncated``. On failure,
        ``status`` "error" with ``error_message``.
    """
    try:
        statement = validate_read_only_sql(sql)
        return _execute(statement, clamp_rows(max_rows))
    except Exception as exc:  # noqa: BLE001 — mapped to a structured error dict
        return error_result(exc, "Redshift", "REDSHIFT_ACCESS_KEY_ID", logger)


def tool_redshift_list_tables(schema: str | None = None, max_rows: int = 500) -> dict:
    """List the tables and views of the configured Redshift database.

    Args:
        schema (str): Restrict to this schema. Optional.
        max_rows (int): Maximum tables to return (1–1000). Default: 500.

    Returns:
        dict: Same shape as ``tool_redshift_run_query``, one row per table
        (``table_schema``, ``table_name``, ``table_type``).
    """
    sql = (
        "SELECT table_schema, table_name, table_type FROM information_schema.tables "
        "WHERE table_schema NOT IN ('information_schema', 'pg_catalog')"
    )
    if schema and schema.strip():
        if not re.match(r"^[A-Za-z0-9_]+$", schema.strip()):
            return {"status": "error", "error_message": "Invalid `schema` name."}
        sql += f" AND table_schema = '{schema.strip()}'"
    sql += " ORDER BY table_schema, table_name"
    try:
        return _execute(sql, clamp_rows(max_rows))
    except Exception as exc:  # noqa: BLE001 — mapped to a structured error dict
        return error_result(exc, "Redshift", "REDSHIFT_ACCESS_KEY_ID", logger)

"""Google BigQuery tools — run read-only SQL and list tables.

API contract
------------
Verified against the BigQuery v2 discovery document
(bigquery.googleapis.com/discovery/v1/apis/bigquery/v2/rest), read 2026-10-06:

* ``POST https://bigquery.googleapis.com/bigquery/v2/projects/{projectId}/queries``
  (jobs.query) with ``query``, ``useLegacySql``, ``maxResults``, ``timeoutMs``,
  ``location``, ``maximumBytesBilled``.
* Response: ``jobComplete``, ``jobReference.{jobId,location}``,
  ``schema.fields[].name``, ``rows[].f[].v``, ``totalRows`` (string). While
  ``jobComplete`` is false, poll ``GET .../projects/{projectId}/queries/{jobId}``
  (jobs.getQueryResults).
* Tables: ``GET .../projects/{projectId}/datasets`` and
  ``GET .../projects/{projectId}/datasets/{datasetId}/tables``.
* Auth: OAuth2 bearer token, scope ``https://www.googleapis.com/auth/bigquery``,
  minted from a service account key with google-auth (already a dependency of
  google-adk). Grant the account BigQuery Data Viewer + BigQuery Job User.

No BigQuery client library is used (plain HTTPS through httpx), so nothing
heavy is loaded in the application pod. Queries go through the shared
read-only guard; ``BIGQUERY_MAX_BYTES_BILLED`` caps the cost of one query.

``BIGQUERY_*`` settings are read at module level so the ToolsStore parameter
scanner surfaces them in the UI, and again at call time.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
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
_BIGQUERY_PROJECT_ID = os.getenv("BIGQUERY_PROJECT_ID", "")
_BIGQUERY_SERVICE_ACCOUNT_JSON = os.getenv("BIGQUERY_SERVICE_ACCOUNT_JSON", "")
_BIGQUERY_LOCATION = os.getenv("BIGQUERY_LOCATION", "")
_BIGQUERY_MAX_BYTES_BILLED = os.getenv("BIGQUERY_MAX_BYTES_BILLED", "")

_BASE_URL = "https://bigquery.googleapis.com/bigquery/v2"
_SCOPE = "https://www.googleapis.com/auth/bigquery"
_HTTP_TIMEOUT_S = 60
_QUERY_TIMEOUT_MS = 30_000
_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")

_creds_lock = threading.Lock()
_creds_cache: dict[str, Any] = {}


def _project() -> str:
    project = (os.environ.get("BIGQUERY_PROJECT_ID") or "").strip()
    if not project:
        raise EnvironmentError("BIGQUERY_PROJECT_ID not set")
    if not _ID_RE.match(project):
        raise EnvironmentError(f"BIGQUERY_PROJECT_ID is not valid: {project!r}")
    return project


def _access_token() -> str:
    """Mint (or reuse) an OAuth token from the service account key."""
    raw = os.environ.get("BIGQUERY_SERVICE_ACCOUNT_JSON") or ""
    if not raw.strip():
        raise EnvironmentError("BIGQUERY_SERVICE_ACCOUNT_JSON not set")
    key = hashlib.sha256(raw.encode()).hexdigest()
    with _creds_lock:
        creds = _creds_cache.get(key)
        if creds is None:
            from google.oauth2 import service_account

            try:
                info = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise EnvironmentError(
                    "BIGQUERY_SERVICE_ACCOUNT_JSON is not valid JSON."
                ) from exc
            creds = service_account.Credentials.from_service_account_info(
                info, scopes=[_SCOPE]
            )
            _creds_cache.clear()
            _creds_cache[key] = creds
        if not creds.valid:
            from google.auth.transport.requests import Request

            creds.refresh(Request())
        return creds.token


def _request(
    method: str, path: str, *, json_body: dict | None = None, params: dict | None = None
) -> dict:
    import httpx

    resp = httpx.request(
        method,
        f"{_BASE_URL}{path}",
        headers={
            "Authorization": f"Bearer {_access_token()}",
            "Content-Type": "application/json",
        },
        json=json_body,
        params=params,
        timeout=_HTTP_TIMEOUT_S,
    )
    resp.raise_for_status()
    return resp.json()


def _cell(value: Any) -> Any:
    """Unwrap BigQuery's ``{"v": ...}`` cells, recursing into records/arrays."""
    if isinstance(value, dict) and "f" in value:
        return [_cell(c.get("v")) for c in value["f"]]
    if isinstance(value, list):
        return [
            _cell(item.get("v") if isinstance(item, dict) else item) for item in value
        ]
    return value


def tool_bigquery_run_query(sql: str, max_rows: int = 100) -> dict:
    """Run a read-only GoogleSQL query on BigQuery.

    Only single SELECT / WITH / SHOW / DESCRIBE / EXPLAIN statements are
    accepted. Reference tables as ``dataset.table`` or
    ``project.dataset.table``.

    Args:
        sql (str): The query (GoogleSQL, not legacy SQL). Required.
        max_rows (int): Maximum rows to return (1–1000). Default: 100.

    Returns:
        dict: On success, ``status`` "success" with ``columns``, ``rows`` (list
        of dicts), ``row_count``, ``total_rows`` and ``truncated``. On failure,
        ``status`` "error" with ``error_message``.
    """
    cap = clamp_rows(max_rows)
    try:
        statement = validate_read_only_sql(sql)
        project = _project()
        body: dict[str, Any] = {
            "query": statement,
            "useLegacySql": False,
            "maxResults": cap,
            "timeoutMs": _QUERY_TIMEOUT_MS,
        }
        location = os.environ.get("BIGQUERY_LOCATION")
        if location:
            body["location"] = location
        max_bytes = (os.environ.get("BIGQUERY_MAX_BYTES_BILLED") or "").strip()
        if max_bytes:
            if not max_bytes.isdigit():
                raise EnvironmentError("BIGQUERY_MAX_BYTES_BILLED must be an integer.")
            body["maximumBytesBilled"] = max_bytes

        data = _request("POST", f"/projects/{project}/queries", json_body=body)
        deadline = time.monotonic() + POLL_BUDGET_S
        while not data.get("jobComplete"):
            job = data.get("jobReference") or {}
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"BigQuery job {job.get('jobId')} still running after "
                    f"{POLL_BUDGET_S}s."
                )
            time.sleep(POLL_INTERVAL_S)
            params: dict[str, Any] = {"maxResults": cap, "timeoutMs": _QUERY_TIMEOUT_MS}
            if job.get("location"):
                params["location"] = job["location"]
            data = _request(
                "GET", f"/projects/{project}/queries/{job.get('jobId')}", params=params
            )
    except Exception as exc:  # noqa: BLE001 — mapped to a structured error dict
        return error_result(exc, "BigQuery", "BIGQUERY_SERVICE_ACCOUNT_JSON", logger)

    columns = [f.get("name") for f in (data.get("schema") or {}).get("fields") or []]
    rows = [
        [_cell(c.get("v")) for c in row.get("f") or []]
        for row in data.get("rows") or []
    ]
    total = data.get("totalRows")
    return rows_result(columns, rows, cap, int(total) if total is not None else None)


def tool_bigquery_list_tables(dataset: str | None = None, max_rows: int = 500) -> dict:
    """List the datasets and tables of the configured BigQuery project.

    Args:
        dataset (str): Restrict to this dataset. Optional — all datasets
            otherwise.
        max_rows (int): Maximum tables to return (1–1000). Default: 500.

    Returns:
        dict: Same shape as ``tool_bigquery_run_query``, one row per table
        (``dataset``, ``table``, ``type``).
    """
    cap = clamp_rows(max_rows)
    try:
        project = _project()
        if dataset and dataset.strip():
            if not _ID_RE.match(dataset.strip()):
                raise ValueError("Invalid `dataset` name.")
            datasets = [dataset.strip()]
        else:
            listing = _request(
                "GET", f"/projects/{project}/datasets", params={"maxResults": 1000}
            )
            datasets = [
                str(ref["datasetId"])
                for d in listing.get("datasets") or []
                if (ref := d.get("datasetReference") or {}).get("datasetId")
            ]
        rows: list[list[Any]] = []
        for ds in datasets:
            if len(rows) > cap:
                break
            tables = _request(
                "GET",
                f"/projects/{project}/datasets/{ds}/tables",
                params={"maxResults": cap + 1},
            )
            for table in tables.get("tables") or []:
                ref = table.get("tableReference") or {}
                rows.append([ds, ref.get("tableId"), table.get("type")])
    except Exception as exc:  # noqa: BLE001 — mapped to a structured error dict
        return error_result(exc, "BigQuery", "BIGQUERY_SERVICE_ACCOUNT_JSON", logger)
    return rows_result(["dataset", "table", "type"], rows, cap, None)

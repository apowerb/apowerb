"""Microsoft Dynamics 365 tools — query, list tables, create and update rows
through the Dataverse Web API.

API contract
------------
Verified against "Use OAuth authentication with Microsoft Dataverse"
(learn.microsoft.com, updated 2026-07-24), consulted 2026-10-06:

* Server-to-server auth: an Entra ID app registration with a client secret,
  bound to a Dataverse *application user* whose security role bounds what the
  agent can read and write. Token from
  ``POST https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token``
  (``grant_type=client_credentials``) with scope ``<environment-url>/.default``.
* Web API base: ``<environment-url>/api/data/v9.2/`` with headers
  ``OData-MaxVersion: 4.0``, ``OData-Version: 4.0``, ``Accept: application/json``.
* Query: ``GET /{entitySet}?$select=&$filter=&$orderby=&$top=`` →
  ``{"value": [...], "@odata.nextLink"?}``. Create: ``POST /{entitySet}`` with
  ``Prefer: return=representation``. Update: ``PATCH /{entitySet}({id})`` with
  ``If-Match: *`` (no upsert). Tables: ``GET /EntityDefinitions``.

``DYNAMICS365_*`` settings are read at module level so the ToolsStore
parameter scanner surfaces them in the UI, and again at call time.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from logging import getLogger
from typing import Any
from urllib.parse import urlsplit

logger = getLogger(__name__)

# Read at module level so the ToolsStore scanner (regex on os.getenv) discovers
# them for the UI. Re-read at call time.
_DYNAMICS365_URL = os.getenv("DYNAMICS365_URL", "")
_DYNAMICS365_TENANT_ID = os.getenv("DYNAMICS365_TENANT_ID", "")
_DYNAMICS365_CLIENT_ID = os.getenv("DYNAMICS365_CLIENT_ID", "")
_DYNAMICS365_CLIENT_SECRET = os.getenv("DYNAMICS365_CLIENT_SECRET", "")

_TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
_API_PATH = "/api/data/v9.2"
_HTTP_TIMEOUT_S = 30
_MAX_ROWS_CAP = 1000
_HOST_RE = re.compile(r"^[A-Za-z0-9-]+\.crm[0-9]*\.dynamics\.com$")
_TENANT_RE = re.compile(r"^[A-Za-z0-9.-]+$")
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")

_token_lock = threading.Lock()
_token_cache: dict[str, tuple[str, float]] = {}


def _env(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise EnvironmentError(f"{name} not set")
    return value


def _base_url() -> str:
    raw = _env("DYNAMICS365_URL")
    host = urlsplit(raw if "://" in raw else f"https://{raw}").hostname or ""
    if not _HOST_RE.match(host):
        raise EnvironmentError(
            f"DYNAMICS365_URL must be an environment URL (*.crm*.dynamics.com): {raw!r}"
        )
    return f"https://{host}"


def _access_token(base: str) -> str:
    """Return a cached app-only token for the environment, minting if needed."""
    import httpx

    tenant = _env("DYNAMICS365_TENANT_ID")
    if not _TENANT_RE.match(tenant):
        raise EnvironmentError("DYNAMICS365_TENANT_ID is not valid.")
    client_id = _env("DYNAMICS365_CLIENT_ID")
    secret = _env("DYNAMICS365_CLIENT_SECRET")
    key = hashlib.sha256(
        f"{base}\0{tenant}\0{client_id}\0{secret}".encode()
    ).hexdigest()
    with _token_lock:
        cached = _token_cache.get(key)
        if cached and cached[1] > time.monotonic():
            return cached[0]
        resp = httpx.post(
            _TOKEN_URL.format(tenant=tenant),
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": secret,
                "scope": f"{base}/.default",
            },
            timeout=_HTTP_TIMEOUT_S,
        )
        resp.raise_for_status()
        data = resp.json()
        token = data["access_token"]
        lifetime = int(data.get("expires_in") or 3600)
        _token_cache.clear()
        # Refresh a minute early so a token never expires mid-request.
        _token_cache[key] = (token, time.monotonic() + max(lifetime - 60, 0))
        return token


def _request(
    method: str,
    path: str,
    *,
    json_body: dict | None = None,
    params: dict | None = None,
    extra_headers: dict | None = None,
) -> Any:
    import httpx

    base = _base_url()
    headers = {
        "Authorization": f"Bearer {_access_token(base)}",
        "OData-MaxVersion": "4.0",
        "OData-Version": "4.0",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    headers.update(extra_headers or {})
    url = path if path.startswith(f"{base}{_API_PATH}/") else f"{base}{_API_PATH}{path}"
    resp = httpx.request(
        method,
        url,
        headers=headers,
        json=json_body,
        params=params,
        timeout=_HTTP_TIMEOUT_S,
    )
    resp.raise_for_status()
    return resp.json() if resp.content else {}


def _error_result(exc: Exception) -> dict:
    import httpx

    if isinstance(exc, (EnvironmentError, ValueError)):
        return {"status": "error", "error_message": str(exc)}
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        detail = exc.response.text[:500]
        if code in (401, 403) or "login.microsoftonline.com" in str(exc.request.url):
            detail = (
                "Authentication failed (check DYNAMICS365_TENANT_ID / CLIENT_ID / "
                f"CLIENT_SECRET and the Dataverse application user). {detail}"
            )
        logger.warning("[DYNAMICS365] HTTP %s: %s", code, detail)
        return {
            "status": "error",
            "http_status": code,
            "error_message": f"Dynamics 365 error (HTTP {code}). {detail}",
        }
    logger.warning("[DYNAMICS365] request failed: %s", exc)
    return {
        "status": "error",
        "error_message": f"Request to Dynamics 365 failed: {exc}",
    }


def _entity_set(value: str | None) -> str:
    if not isinstance(value, str) or not _NAME_RE.match(value.strip()):
        raise ValueError("`entity_set` must be an entity set name such as 'accounts'.")
    return value.strip()


def _clean(row: dict) -> dict:
    """Drop OData annotations (``@odata.etag`` and friends)."""
    return {k: v for k, v in row.items() if not k.startswith("@")}


def tool_dynamics365_query(
    entity_set: str,
    select: str | None = None,
    filter: str | None = None,
    orderby: str | None = None,
    max_rows: int = 100,
) -> dict:
    """Query rows of a Dynamics 365 / Dataverse table (OData).

    Args:
        entity_set (str): Entity set name (plural), e.g. "accounts",
            "contacts", "opportunities", "leads". Required.
        select (str): Comma-separated columns, e.g. "name,revenue". Optional
            but recommended.
        filter (str): OData filter, e.g. "statecode eq 0 and revenue gt 10000".
            Optional.
        orderby (str): OData order, e.g. "createdon desc". Optional.
        max_rows (int): Maximum rows to return (1–1000). Default: 100.

    Returns:
        dict: On success, ``status`` "success" with ``count``, ``truncated``
        and ``rows``. On failure, ``status`` "error" with ``error_message``.
    """
    cap = max(1, min(max_rows, _MAX_ROWS_CAP))
    try:
        params: dict[str, Any] = {"$top": cap + 1}
        if select:
            params["$select"] = select
        if filter:
            params["$filter"] = filter
        if orderby:
            params["$orderby"] = orderby
        data = _request("GET", f"/{_entity_set(entity_set)}", params=params)
    except Exception as exc:  # noqa: BLE001 — mapped to a structured error dict
        return _error_result(exc)

    rows = data.get("value") or []
    return {
        "status": "success",
        "count": min(len(rows), cap),
        "truncated": len(rows) > cap or bool(data.get("@odata.nextLink")),
        "rows": [_clean(r) for r in rows[:cap]],
    }


def tool_dynamics365_list_tables(custom_only: bool = False) -> dict:
    """List Dataverse tables with their entity set names (for queries).

    Args:
        custom_only (bool): Only custom tables. Default: False.

    Returns:
        dict: On success, ``status`` "success" with ``count`` and ``tables``
        (``logical_name``, ``entity_set``, ``custom``). On failure, ``status``
        "error" with ``error_message``.
    """
    params = {"$select": "LogicalName,EntitySetName,IsCustomEntity"}
    if custom_only:
        params["$filter"] = "IsCustomEntity eq true"
    try:
        data = _request("GET", "/EntityDefinitions", params=params)
    except Exception as exc:  # noqa: BLE001 — mapped to a structured error dict
        return _error_result(exc)
    tables = [
        {
            "logical_name": t.get("LogicalName"),
            "entity_set": t.get("EntitySetName"),
            "custom": t.get("IsCustomEntity"),
        }
        for t in data.get("value") or []
        if t.get("EntitySetName")
    ]
    return {"status": "success", "count": len(tables), "tables": tables}


def tool_dynamics365_create_record(entity_set: str, fields: dict) -> dict:
    """Create a row in a Dynamics 365 / Dataverse table.

    Args:
        entity_set (str): Entity set name, e.g. "leads". Required.
        fields (dict): Column logical names and values, e.g.
            ``{"subject": "Demo", "lastname": "Doe"}``. Lookups use
            ``{"parentcustomerid_account@odata.bind": "/accounts(<id>)"}``.
            Required.

    Returns:
        dict: On success, ``status`` "success" with ``record`` (the created
        row). On failure, ``status`` "error" with ``error_message``.
    """
    if not isinstance(fields, dict) or not fields:
        return {
            "status": "error",
            "error_message": "`fields` must be a non-empty dict.",
        }
    try:
        data = _request(
            "POST",
            f"/{_entity_set(entity_set)}",
            json_body=fields,
            extra_headers={"Prefer": "return=representation"},
        )
    except Exception as exc:  # noqa: BLE001 — mapped to a structured error dict
        return _error_result(exc)
    return {"status": "success", "record": _clean(data)}


def tool_dynamics365_update_record(
    entity_set: str, record_id: str, fields: dict
) -> dict:
    """Update columns of an existing Dynamics 365 / Dataverse row.

    Args:
        entity_set (str): Entity set name, e.g. "opportunities". Required.
        record_id (str): The row GUID. Required.
        fields (dict): Column logical names and new values. Required.

    Returns:
        dict: On success, ``status`` "success" with ``id``. On failure,
        ``status`` "error" with ``error_message``.
    """
    if not isinstance(fields, dict) or not fields:
        return {
            "status": "error",
            "error_message": "`fields` must be a non-empty dict.",
        }
    if not isinstance(record_id, str) or not _GUID_RE.match(record_id.strip()):
        return {"status": "error", "error_message": "`record_id` must be a GUID."}
    try:
        _request(
            "PATCH",
            f"/{_entity_set(entity_set)}({record_id.strip()})",
            json_body=fields,
            # Without If-Match a PATCH on a missing id would create the row.
            extra_headers={"If-Match": "*"},
        )
    except Exception as exc:  # noqa: BLE001 — mapped to a structured error dict
        return _error_result(exc)
    return {"status": "success", "id": record_id.strip()}

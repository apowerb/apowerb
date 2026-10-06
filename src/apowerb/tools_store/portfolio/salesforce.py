"""Salesforce CRM tools — SOQL queries, object descriptions, create and update.

API contract
------------
Verified against the Salesforce OAuth 2.0 client credentials flow
documentation (help.salesforce.com, "OAuth 2.0 Client Credentials Flow for
Server-to-Server Integration"), consulted 2026-10-06:

* Token: ``POST https://<my-domain>.my.salesforce.com/services/oauth2/token``
  form-encoded ``grant_type=client_credentials``, ``client_id`` (consumer key),
  ``client_secret`` (consumer secret). Response: ``access_token``,
  ``instance_url``, ``token_type`` "Bearer". The connected app must enable the
  client credentials flow and name a "Run As" integration user — its
  permissions bound what the agent can see and change.
* REST API under ``{instance_url}/services/data/{version}``:
  ``GET /query?q=<SOQL>`` → ``{totalSize, done, records, nextRecordsUrl}``;
  ``GET /sobjects`` and ``GET /sobjects/{name}/describe``;
  ``POST /sobjects/{name}`` → ``{id, success, errors}``;
  ``PATCH /sobjects/{name}/{id}`` → 204.
* Errors: a list of ``{message, errorCode, fields}``.

``SALESFORCE_*`` settings are read at module level so the ToolsStore parameter
scanner surfaces them in the UI, and again at call time.
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
_SALESFORCE_DOMAIN = os.getenv("SALESFORCE_DOMAIN", "")
_SALESFORCE_CLIENT_ID = os.getenv("SALESFORCE_CLIENT_ID", "")
_SALESFORCE_CLIENT_SECRET = os.getenv("SALESFORCE_CLIENT_SECRET", "")
_SALESFORCE_API_VERSION = os.getenv("SALESFORCE_API_VERSION", "v62.0")

_HTTP_TIMEOUT_S = 30
_TOKEN_TTL_S = 30 * 60  # Salesforce returns no expires_in; re-auth on 401 too.
_MAX_ROWS_CAP = 2000
_HOST_RE = re.compile(r"^[A-Za-z0-9.-]+\.(salesforce|force)\.com$")
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_ID_RE = re.compile(r"^[A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?$")
_VERSION_RE = re.compile(r"^v\d+\.\d$")

_token_lock = threading.Lock()
_token_cache: dict[str, tuple[str, str, float]] = {}


def _env(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise EnvironmentError(f"{name} not set")
    return value


def _login_host() -> str:
    raw = _env("SALESFORCE_DOMAIN")
    host = urlsplit(raw if "://" in raw else f"https://{raw}").hostname or ""
    if "." not in host:
        host = f"{host}.my.salesforce.com"
    if not _HOST_RE.match(host):
        raise EnvironmentError(
            f"SALESFORCE_DOMAIN must be a My Domain host (*.my.salesforce.com): {raw!r}"
        )
    return host


def _api_version() -> str:
    version = (os.environ.get("SALESFORCE_API_VERSION") or "v62.0").strip()
    if not version.startswith("v"):
        version = f"v{version}"
    if not _VERSION_RE.match(version):
        raise EnvironmentError(f"SALESFORCE_API_VERSION is not valid: {version!r}")
    return version


def _session(force_refresh: bool = False) -> tuple[str, str]:
    """Return ``(access_token, instance_url)``, cached per credentials."""
    import httpx

    host = _login_host()
    client_id = _env("SALESFORCE_CLIENT_ID")
    secret = _env("SALESFORCE_CLIENT_SECRET")
    key = hashlib.sha256(f"{host}\0{client_id}\0{secret}".encode()).hexdigest()
    with _token_lock:
        cached = _token_cache.get(key)
        if cached and not force_refresh and cached[2] > time.monotonic():
            return cached[0], cached[1]
        resp = httpx.post(
            f"https://{host}/services/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": secret,
            },
            timeout=_HTTP_TIMEOUT_S,
        )
        resp.raise_for_status()
        data = resp.json()
        instance = urlsplit(data.get("instance_url") or "").hostname or ""
        if not _HOST_RE.match(instance):
            raise EnvironmentError("Salesforce returned an unexpected instance_url.")
        session = (data["access_token"], f"https://{instance}")
        _token_cache.clear()
        _token_cache[key] = (*session, time.monotonic() + _TOKEN_TTL_S)
        return session


def _request(
    method: str, path: str, *, json_body: dict | None = None, params: dict | None = None
) -> Any:
    """Call the REST API (``path`` relative to /services/data/<version>)."""
    import httpx

    for attempt in (0, 1):
        token, instance = _session(force_refresh=attempt == 1)
        url = (
            path
            if path.startswith("/services/")
            else (f"/services/data/{_api_version()}{path}")
        )
        resp = httpx.request(
            method,
            f"{instance}{url}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json=json_body,
            params=params,
            timeout=_HTTP_TIMEOUT_S,
        )
        if resp.status_code == 401 and attempt == 0:
            continue  # expired session: mint a new token once
        resp.raise_for_status()
        return resp.json() if resp.content else {}
    raise RuntimeError("unreachable")


def _error_result(exc: Exception) -> dict:
    import httpx

    if isinstance(exc, (EnvironmentError, ValueError)):
        return {"status": "error", "error_message": str(exc)}
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        detail = exc.response.text[:500]
        if code in (400, 401) and "oauth2/token" in str(exc.request.url):
            detail = (
                "Authentication failed (check SALESFORCE_CLIENT_ID / "
                f"SALESFORCE_CLIENT_SECRET and the client credentials flow). {detail}"
            )
        logger.warning("[SALESFORCE] HTTP %s: %s", code, detail)
        return {
            "status": "error",
            "http_status": code,
            "error_message": f"Salesforce error (HTTP {code}). {detail}",
        }
    logger.warning("[SALESFORCE] request failed: %s", exc)
    return {"status": "error", "error_message": f"Request to Salesforce failed: {exc}"}


def _clean(record: dict) -> dict:
    """Drop the ``attributes`` envelope, recursively for relationship fields."""
    return {
        k: _clean(v) if isinstance(v, dict) else v
        for k, v in record.items()
        if k != "attributes"
    }


def _check_name(value: str | None, what: str) -> str:
    if not isinstance(value, str) or not _NAME_RE.match(value.strip()):
        raise ValueError(f"`{what}` must be an API name such as 'Account'.")
    return value.strip()


def tool_salesforce_query(soql: str, max_rows: int = 200) -> dict:
    """Run a SOQL query on Salesforce (read-only by nature).

    Args:
        soql (str): The SOQL query, e.g.
            "SELECT Id, Name, Amount FROM Opportunity WHERE IsClosed = false".
            Required.
        max_rows (int): Maximum records to return (1–2000). Default: 200.

    Returns:
        dict: On success, ``status`` "success" with ``total_size``, ``count``,
        ``truncated`` and ``records``. On failure, ``status`` "error" with
        ``error_message``.
    """
    if not isinstance(soql, str) or not soql.strip():
        return {"status": "error", "error_message": "`soql` is required."}
    cap = max(1, min(max_rows, _MAX_ROWS_CAP))
    try:
        data = _request("GET", "/query", params={"q": soql.strip()})
        records = list(data.get("records") or [])
        while len(records) < cap and not data.get("done", True):
            next_url = data.get("nextRecordsUrl")
            if not next_url or not next_url.startswith("/services/data/"):
                break
            data = _request("GET", next_url)
            records.extend(data.get("records") or [])
    except Exception as exc:  # noqa: BLE001 — mapped to a structured error dict
        return _error_result(exc)

    total = data.get("totalSize")
    kept = [_clean(r) for r in records[:cap]]
    return {
        "status": "success",
        "total_size": total,
        "count": len(kept),
        "truncated": isinstance(total, int) and total > len(kept),
        "records": kept,
    }


def tool_salesforce_describe_object(sobject: str | None = None) -> dict:
    """List Salesforce objects, or describe the fields of one object.

    Args:
        sobject (str): API name of the object (e.g. "Account"). Omit to list
            the queryable objects.

    Returns:
        dict: Without ``sobject``: ``objects`` (``name``, ``label``,
        ``custom``). With it: ``fields`` (``name``, ``label``, ``type``,
        ``createable``, ``updateable``, ``picklist_values``). On failure,
        ``status`` "error" with ``error_message``.
    """
    try:
        if not sobject:
            data = _request("GET", "/sobjects")
            objects = [
                {
                    "name": o.get("name"),
                    "label": o.get("label"),
                    "custom": o.get("custom"),
                }
                for o in data.get("sobjects") or []
                if o.get("queryable")
            ]
            return {"status": "success", "count": len(objects), "objects": objects}
        name = _check_name(sobject, "sobject")
        data = _request("GET", f"/sobjects/{name}/describe")
    except Exception as exc:  # noqa: BLE001 — mapped to a structured error dict
        return _error_result(exc)

    fields = [
        {
            "name": f.get("name"),
            "label": f.get("label"),
            "type": f.get("type"),
            "createable": f.get("createable"),
            "updateable": f.get("updateable"),
            "picklist_values": [
                p.get("value") for p in f.get("picklistValues") or [] if p.get("active")
            ],
        }
        for f in data.get("fields") or []
    ]
    return {"status": "success", "name": data.get("name"), "fields": fields}


def tool_salesforce_create_record(sobject: str, fields: dict) -> dict:
    """Create a Salesforce record (Account, Contact, Lead, Opportunity, ...).

    Args:
        sobject (str): API name of the object, e.g. "Lead". Required.
        fields (dict): Field API names and values, e.g.
            ``{"LastName": "Doe", "Company": "Acme"}``. Required.

    Returns:
        dict: On success, ``status`` "success" with ``id``. On failure,
        ``status`` "error" with ``error_message``.
    """
    if not isinstance(fields, dict) or not fields:
        return {
            "status": "error",
            "error_message": "`fields` must be a non-empty dict.",
        }
    try:
        name = _check_name(sobject, "sobject")
        data = _request("POST", f"/sobjects/{name}", json_body=fields)
    except Exception as exc:  # noqa: BLE001 — mapped to a structured error dict
        return _error_result(exc)
    return {"status": "success", "id": data.get("id")}


def tool_salesforce_update_record(sobject: str, record_id: str, fields: dict) -> dict:
    """Update fields of an existing Salesforce record.

    Args:
        sobject (str): API name of the object, e.g. "Opportunity". Required.
        record_id (str): The 15- or 18-character record ID. Required.
        fields (dict): Field API names and new values. Required.

    Returns:
        dict: On success, ``status`` "success" with ``id``. On failure,
        ``status`` "error" with ``error_message``.
    """
    if not isinstance(fields, dict) or not fields:
        return {
            "status": "error",
            "error_message": "`fields` must be a non-empty dict.",
        }
    if not isinstance(record_id, str) or not _ID_RE.match(record_id.strip()):
        return {
            "status": "error",
            "error_message": "`record_id` is not a Salesforce ID.",
        }
    try:
        name = _check_name(sobject, "sobject")
        _request("PATCH", f"/sobjects/{name}/{record_id.strip()}", json_body=fields)
    except Exception as exc:  # noqa: BLE001 — mapped to a structured error dict
        return _error_result(exc)
    return {"status": "success", "id": record_id.strip()}

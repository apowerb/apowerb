"""Generic Odoo tools exposed to agents.

Each tool is a thin wrapper around Odoo's JSON-RPC ``object.execute_kw`` and
targets any Odoo model (``res.partner``, ``sale.order``, ``crm.lead``, ...).

Credentials are fetched lazily from the per-user ``integrations`` row
(``provider='odoo'``) based on the AGENT_OWNER env var set by the runner.
They are cached in-process so the DB is hit at most once per agent session.
"""

import logging
import os
from typing import Any, Optional

import httpx

from th2agent.helpers.encryptor import decrypt_value

logger = logging.getLogger(__name__)

_JSONRPC_TIMEOUT = 20.0

# Per-process creds cache, keyed by AGENT_OWNER. Each value is a dict:
#   {"url", "database", "login", "api_key", "uid"} — all plain strings/ints.
_creds_cache: dict[str, dict] = {}


def _load_creds() -> dict:
    """Return decrypted Odoo credentials for the current agent owner, caching them."""
    owner = os.getenv("AGENT_OWNER") or ""
    cached = _creds_cache.get(owner)
    if cached:
        return cached

    from th2agent.integrations.helpers import fetch_integration_configs

    configs = fetch_integration_configs("odoo")
    meta = configs.get("meta") or {}
    encrypted_key = configs.get("access_token") or ""

    try:
        api_key = decrypt_value(encrypted_key) if encrypted_key else ""
    except Exception as exc:
        raise RuntimeError(
            f"Failed to decrypt Odoo api_key: {exc}"
        ) from exc

    url = (meta.get("url") or "").rstrip("/")
    database = meta.get("database") or ""
    login = configs.get("provider_username") or ""
    uid_raw = configs.get("provider_user_id") or ""

    if not (url and database and login and api_key and uid_raw):
        raise RuntimeError(
            "Odoo integration is incomplete. Reconnect from the Integrations page."
        )

    try:
        uid = int(uid_raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Odoo integration uid is invalid: {uid_raw!r}") from exc

    creds = {"url": url, "database": database, "login": login, "api_key": api_key, "uid": uid}
    _creds_cache[owner] = creds
    return creds


def reset_odoo_creds_cache(owner: Optional[str] = None) -> None:
    """Clear the creds cache — called when the integration is disconnected/updated."""
    if owner is None:
        _creds_cache.clear()
    else:
        _creds_cache.pop(owner, None)


def _execute_kw(
    creds: dict,
    model: str,
    method: str,
    args: list,
    kwargs: Optional[dict] = None,
) -> Any:
    """Call object.execute_kw via JSON-RPC and return the parsed result."""
    payload = {
        "jsonrpc": "2.0",
        "method":  "call",
        "params": {
            "service": "object",
            "method":  "execute_kw",
            "args": [
                creds["database"],
                creds["uid"],
                creds["api_key"],
                model,
                method,
                args,
                kwargs or {},
            ],
        },
    }
    endpoint = f"{creds['url']}/jsonrpc"
    resp = httpx.post(
        endpoint,
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=_JSONRPC_TIMEOUT,
    )
    resp.raise_for_status()
    body = resp.json()
    if "error" in body:
        err = body["error"]
        data = err.get("data") or {}
        msg = data.get("message") or err.get("message") or str(err)
        raise RuntimeError(f"Odoo error on {model}.{method}: {msg}")
    return body.get("result")


# ---------------------------------------------------------------------------
# Tools exposed to agents
# ---------------------------------------------------------------------------


def tool_odoo_search_records(
    model: str,
    domain: Optional[list] = None,
    fields: Optional[list] = None,
    limit: int = 50,
    offset: int = 0,
    order: Optional[str] = None,
) -> dict:
    """Search records in an Odoo model and return their fields.

    Uses Odoo's ``search_read`` which combines search + read in one round trip.

    Args:
        model:  Odoo model name, e.g. "res.partner", "sale.order", "crm.lead".
        domain: Odoo domain expression as a list of tuples, e.g.
                ``[("is_company", "=", True), ("country_id.code", "=", "FR")]``.
                Pass an empty list (or None) to match all records.
        fields: Field names to retrieve. When None, Odoo returns a default set.
        limit:  Max number of records to return (default 50, hard-capped at 500).
        offset: Number of records to skip (for pagination).
        order:  SQL-like ordering, e.g. "name asc" or "create_date desc".

    Returns:
        {"success": True, "records": [...]} on success, or
        {"success": False, "error": "..."} on failure.
    """
    try:
        creds = _load_creds()
        safe_limit = max(1, min(int(limit or 50), 500))
        kwargs: dict = {"limit": safe_limit, "offset": max(0, int(offset or 0))}
        if fields:
            kwargs["fields"] = list(fields)
        if order:
            kwargs["order"] = order
        records = _execute_kw(
            creds,
            model,
            "search_read",
            [domain or []],
            kwargs,
        )
        return {"success": True, "records": records or []}
    except Exception as exc:
        logger.warning("[odoo] search_read %s failed: %s", model, exc)
        return {"success": False, "error": str(exc)}


def tool_odoo_read_records(
    model: str,
    ids: list,
    fields: Optional[list] = None,
) -> dict:
    """Read specific records by id from an Odoo model.

    Args:
        model:  Odoo model name.
        ids:    List of record ids to read, e.g. [12, 34, 56].
        fields: Field names to retrieve. When None, Odoo returns its default set.

    Returns:
        {"success": True, "records": [...]} or {"success": False, "error": "..."}.
    """
    try:
        creds = _load_creds()
        kwargs: dict = {}
        if fields:
            kwargs["fields"] = list(fields)
        records = _execute_kw(creds, model, "read", [list(ids or [])], kwargs)
        return {"success": True, "records": records or []}
    except Exception as exc:
        logger.warning("[odoo] read %s failed: %s", model, exc)
        return {"success": False, "error": str(exc)}


def tool_odoo_create_record(model: str, values: dict) -> dict:
    """Create a new record in an Odoo model.

    Args:
        model:  Odoo model name, e.g. "res.partner".
        values: Field-to-value mapping for the new record.

    Returns:
        {"success": True, "id": <int>} on success, or
        {"success": False, "error": "..."} on failure.
    """
    try:
        creds = _load_creds()
        new_id = _execute_kw(creds, model, "create", [dict(values or {})])
        return {"success": True, "id": new_id}
    except Exception as exc:
        logger.warning("[odoo] create %s failed: %s", model, exc)
        return {"success": False, "error": str(exc)}


def tool_odoo_update_record(model: str, ids: list, values: dict) -> dict:
    """Update existing records in an Odoo model.

    Args:
        model:  Odoo model name.
        ids:    List of record ids to update.
        values: Field-to-value mapping to apply.

    Returns:
        {"success": True, "updated": True} on success, or
        {"success": False, "error": "..."} on failure.
    """
    try:
        creds = _load_creds()
        ok = _execute_kw(creds, model, "write", [list(ids or []), dict(values or {})])
        return {"success": bool(ok), "updated": bool(ok)}
    except Exception as exc:
        logger.warning("[odoo] write %s failed: %s", model, exc)
        return {"success": False, "error": str(exc)}

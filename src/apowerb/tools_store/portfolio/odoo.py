"""Generic Odoo tools exposed to agents.

Each tool is a thin wrapper around Odoo's JSON-RPC ``object.execute_kw`` and
targets any Odoo model (``res.partner``, ``sale.order``, ``crm.lead``, ...).

Credentials are fetched lazily from the per-user ``integrations`` row
(``provider='odoo'``) of the current invoker. They are cached in-process,
keyed by that invoker, so the DB is hit at most once per user per process.
"""

import logging
from typing import Any, Optional

import httpx

from apowerb.helpers.encryptor import decrypt_value
from apowerb.tools_store.bi_source import bi_source

logger = logging.getLogger(__name__)

_JSONRPC_TIMEOUT = 20.0

# Fields returned when the agent does not name any (a full Odoo record can hold
# hundreds of fields and would flood the model context).
_DEFAULT_FIELDS = ("id", "display_name")
_MAX_READ_IDS = 200

# Per-process creds cache, keyed by the invoker. Each value is a dict:
#   {"url", "database", "login", "api_key", "uid"} — all plain strings/ints.
_creds_cache: dict[str, dict] = {}


def _load_creds() -> dict:
    """Return decrypted Odoo credentials for the current invoker, caching them.

    Keyed by the invoker (``resolve_integration_user(prefer_invoker=True)``) --
    the same identity ``fetch_integration_configs`` resolves the row for. Keying
    by the agent owner instead would hand the first invoker's credentials to
    every later invoker of a shared agent (cross-tenant leak).
    """
    from apowerb.core.invocation_context import resolve_integration_user
    owner = resolve_integration_user(prefer_invoker=True) or ""
    cached = _creds_cache.get(owner)
    if cached:
        return cached

    from apowerb.integrations.helpers import fetch_integration_configs

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
    limit: int = 20,
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
        fields: Field names to retrieve. When None, only ``["id", "display_name"]``
                are returned (to keep the response small): pass the fields you
                need, e.g. ``["name", "email", "country_id"]``.
        limit:  Max number of records to return (default 20, hard-capped at 500).
        offset: Number of records to skip (for pagination).
        order:  SQL-like ordering, e.g. "name asc" or "create_date desc".

    Returns:
        {"success": True, "records": [...]} on success, or
        {"success": False, "error": "..."} on failure.
    """
    try:
        creds = _load_creds()
        safe_limit = max(1, min(int(limit or 20), 500))
        kwargs: dict = {"limit": safe_limit, "offset": max(0, int(offset or 0))}
        kwargs["fields"] = list(fields) if fields else list(_DEFAULT_FIELDS)
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


def _table_cell(value: Any) -> Any:
    """Odoo many2one ``[id, "name"]`` → its name; other lists are kept as they are."""
    if isinstance(value, (list, tuple)) and len(value) == 2 and isinstance(value[0], int) and isinstance(value[1], str):
        return value[1]
    return value


@bi_source(credentials="integration")
def tool_odoo_search_table(
    model: str,
    fields: list,
    domain: Optional[list] = None,
    limit: int = 500,
    order: Optional[str] = None,
) -> dict:
    """Read Odoo records as a table (read-only; also a BI dashboard data source).

    Args:
        model:  Odoo model name, e.g. "sale.order", "crm.lead", "account.move".
        fields: Field names to return as columns (required, so columns are stable).
        domain: Odoo domain, e.g. ``[["state", "=", "sale"]]``. Empty = all records.
        limit:  Max records (1-500). Default 500.
        order:  SQL-like ordering, e.g. "date_order desc".

    Returns:
        {"success": True, "columns": [...], "rows": [{field: value}, ...]} (many2one
        fields are given as the related record's name), or
        {"success": False, "error": "..."}.
    """
    if not fields:
        return {"success": False, "error": "`fields` is required (list of the columns to read)."}
    result = tool_odoo_search_records(model, domain=domain, fields=list(fields), limit=limit, order=order)
    if not result.get("success"):
        return {"success": False, "error": result.get("error") or "Odoo read failed"}
    rows = [{k: _table_cell(v) for k, v in rec.items()} for rec in result.get("records") or []]
    return {"success": True, "columns": ["id", *[f for f in fields if f != "id"]], "rows": rows}


def tool_odoo_read_records(
    model: str,
    ids: list,
    fields: Optional[list] = None,
) -> dict:
    """Read specific records by id from an Odoo model.

    Args:
        model:  Odoo model name.
        ids:    List of record ids to read, e.g. [12, 34, 56] (at most 200 are read).
        fields: Field names to retrieve. When None, only ``["id", "display_name"]``
                are returned: pass the fields you need.

    Returns:
        {"success": True, "records": [...]} or {"success": False, "error": "..."}.
        ``ids_truncated`` is true when more than 200 ids were passed.
    """
    try:
        creds = _load_creds()
        id_list = list(ids or [])
        kwargs: dict = {"fields": list(fields) if fields else list(_DEFAULT_FIELDS)}
        records = _execute_kw(creds, model, "read", [id_list[:_MAX_READ_IDS]], kwargs)
        result: dict = {"success": True, "records": records or []}
        if len(id_list) > _MAX_READ_IDS:
            result["ids_truncated"] = True
        return result
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

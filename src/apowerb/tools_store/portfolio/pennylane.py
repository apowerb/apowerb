"""Pennylane tools — read invoices, bank transactions and the trial balance as tables.

Read-only tools for BI dashboards (``@bi_source``): one row per object, flat columns
(ISO dates, numeric amounts). There is deliberately no write or send tool here.

API contract
------------
NOT verified against the online documentation and never called for real: paths,
parameters and field names below come from the authors' knowledge of the Pennylane
public API v2 and may need adjusting on first contact with a real account.

* Base URL: ``https://app.pennylane.com/api/external/v2``.
* Auth: a company API token in ``Authorization: Bearer <token>``.
* Lists: ``GET /customer_invoices``, ``GET /supplier_invoices``, ``GET /transactions``,
  ``GET /trial_balance``; cursor pagination with ``limit`` (max 100) and ``cursor``,
  answer ``{"items": [...], "has_more": bool, "next_cursor": str|null}``.
* Filter: ``filter`` query parameter, a JSON list of
  ``{"field", "operator", "value"}`` (operators ``gteq`` / ``lteq`` on ``date``).
* Trial balance: ``period_start`` / ``period_end`` query parameters.

``PENNYLANE_API_TOKEN`` is read at module level so the ToolsStore parameter scanner
surfaces it as a UI setting; it is re-read at call time (BI sources inject the
dashboard owner's tool_config through the environment, for the duration of the call).
"""

from __future__ import annotations

import json
import os
import re
from logging import getLogger
from typing import Any

import httpx

from apowerb.tools_store.bi_source import bi_source

logger = getLogger(__name__)

# Read at module level so the ToolsStore scanner (regex on os.getenv) discovers it.
_PENNYLANE_API_TOKEN = os.getenv("PENNYLANE_API_TOKEN", "")

_BASE_URL = "https://app.pennylane.com/api/external/v2"
_HTTP_TIMEOUT_S = 30
_PAGE = 100  # documented per-page maximum
_MAX_ROWS = 10_000
_MAX_PAGES = 120  # hard stop on pagination, whatever the API answers
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

INVOICE_COLUMNS = [
    "id", "invoice_number", "label", "date", "deadline", "currency",
    "amount", "amount_before_tax", "tax", "remaining_amount", "status", "paid",
]  # fmt: skip


def _token() -> str:
    token = os.environ.get("PENNYLANE_API_TOKEN") or ""
    if not token:
        raise EnvironmentError("PENNYLANE_API_TOKEN not set (company API token of the Pennylane account)")
    return token


def _num(value: Any) -> float | None:
    """Amounts come as decimal strings: numeric or None."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _day(value: Any) -> str | None:
    """ISO date (``YYYY-MM-DD``) from a date or datetime string."""
    return str(value)[:10] if value else None


def _pick(obj: dict, *keys: str) -> Any:
    for key in keys:
        if obj.get(key) is not None:
            return obj[key]
    return None


def _party(obj: dict, key: str) -> tuple[Any, Any]:
    """``customer`` / ``supplier`` / ``bank_account``: nested object or bare id -> (id, name)."""
    value = obj.get(key)
    if isinstance(value, dict):
        return value.get("id"), value.get("name") or value.get("label")
    return value, None


def _failure(exc: Exception) -> dict:
    """Map an exception to the tool's error dict; the token never appears in it."""
    if isinstance(exc, (EnvironmentError, ValueError)):
        return {"success": False, "error": str(exc)}
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        detail = exc.response.text[:200].replace(os.environ.get("PENNYLANE_API_TOKEN") or "\0", "***")
        hint = " (check PENNYLANE_API_TOKEN)" if code in (401, 403) else ""
        logger.warning("[PENNYLANE] HTTP %s", code)
        return {"success": False, "error": f"Pennylane error (HTTP {code}){hint}. {detail}".strip()}
    logger.warning("[PENNYLANE] request failed: %s", type(exc).__name__)
    return {"success": False, "error": f"Request to Pennylane failed: {type(exc).__name__}"}


def _date_filter(date_from: str | None, date_to: str | None) -> str | None:
    clauses = []
    for name, value, operator in (("date_from", date_from, "gteq"), ("date_to", date_to, "lteq")):
        if value:
            if not _DATE.match(str(value)):
                raise ValueError(f"{name} must be an ISO date (YYYY-MM-DD), got {value!r}")
            clauses.append({"field": "date", "operator": operator, "value": str(value)})
    return json.dumps(clauses) if clauses else None


def _fetch(path: str, limit: int, params: dict[str, Any] | None = None) -> list[dict]:
    """Up to ``limit`` items of a cursor-paginated list (GET only, bounded)."""
    wanted = max(1, min(int(limit or 1000), _MAX_ROWS))
    headers = {"Authorization": f"Bearer {_token()}", "Accept": "application/json"}
    items: list[dict] = []
    cursor: str | None = None
    for _ in range(_MAX_PAGES):
        query: dict[str, Any] = {**(params or {}), "limit": min(_PAGE, wanted - len(items))}
        if cursor:
            query["cursor"] = cursor
        resp = httpx.get(f"{_BASE_URL}{path}", headers=headers, params=query, timeout=_HTTP_TIMEOUT_S)
        resp.raise_for_status()
        data = resp.json()
        page = [i for i in (data.get("items") or []) if isinstance(i, dict)]
        items.extend(page)
        nxt = data.get("next_cursor")
        if len(items) >= wanted or not data.get("has_more") or not page or not nxt or nxt == cursor:
            break
        cursor = str(nxt)
    return items[:wanted]


def _invoice_rows(items: list[dict], party_key: str) -> list[dict]:
    rows = []
    for inv in items:
        pid, pname = _party(inv, party_key)
        rows.append(
            {
                "id": inv.get("id"),
                "invoice_number": inv.get("invoice_number"),
                "label": inv.get("label"),
                "date": _day(inv.get("date")),
                "deadline": _day(inv.get("deadline")),
                "currency": inv.get("currency"),
                "amount": _num(_pick(inv, "currency_amount", "amount")),
                "amount_before_tax": _num(_pick(inv, "currency_amount_before_tax", "amount_before_tax")),
                "tax": _num(_pick(inv, "currency_tax", "tax")),
                "remaining_amount": _num(_pick(inv, "remaining_amount_with_tax", "remaining_amount")),
                "status": inv.get("status"),
                "paid": inv.get("paid"),
                f"{party_key}_id": pid,
                f"{party_key}_name": pname,
                "updated_at": inv.get("updated_at"),
            }
        )
    return rows


def _list_invoices(path: str, party_key: str, limit: int, date_from: str | None, date_to: str | None) -> dict:
    try:
        flt = _date_filter(date_from, date_to)
        items = _fetch(path, limit, {"filter": flt} if flt else None)
    except Exception as exc:  # noqa: BLE001 — mapped to a structured error dict
        return _failure(exc)
    columns = [*INVOICE_COLUMNS, f"{party_key}_id", f"{party_key}_name", "updated_at"]
    return {"success": True, "columns": columns, "rows": _invoice_rows(items, party_key)}


@bi_source
def tool_pennylane_list_customer_invoices(limit: int = 1000, date_from: str | None = None, date_to: str | None = None) -> dict:
    """List Pennylane customer (sales) invoices as a table (read-only; also a BI dashboard data source).

    Args:
        limit (int): Maximum number of invoices (1-10000). Default: 1000.
        date_from (str): Keep invoices dated on or after this ISO date (YYYY-MM-DD).
        date_to (str): Keep invoices dated on or before this ISO date (YYYY-MM-DD).

    Returns:
        dict: ``{"success": True, "columns": [...], "rows": [...]}`` — one row per invoice:
        id, invoice_number, label, date, deadline, currency, amount (incl. tax),
        amount_before_tax, tax, remaining_amount, status, paid, customer_id,
        customer_name, updated_at — or ``{"success": False, "error": "..."}``.
    """
    return _list_invoices("/customer_invoices", "customer", limit, date_from, date_to)


@bi_source
def tool_pennylane_list_supplier_invoices(limit: int = 1000, date_from: str | None = None, date_to: str | None = None) -> dict:
    """List Pennylane supplier (purchase) invoices as a table (read-only; also a BI dashboard data source).

    Args:
        limit (int): Maximum number of invoices (1-10000). Default: 1000.
        date_from (str): Keep invoices dated on or after this ISO date (YYYY-MM-DD).
        date_to (str): Keep invoices dated on or before this ISO date (YYYY-MM-DD).

    Returns:
        dict: ``{"success": True, "columns": [...], "rows": [...]}`` — same columns as the
        customer invoices with supplier_id and supplier_name instead of the customer ones —
        or ``{"success": False, "error": "..."}``.
    """
    return _list_invoices("/supplier_invoices", "supplier", limit, date_from, date_to)


@bi_source
def tool_pennylane_list_bank_transactions(limit: int = 1000, date_from: str | None = None, date_to: str | None = None) -> dict:
    """List Pennylane bank transactions as a table (read-only; also a BI dashboard data source).

    Args:
        limit (int): Maximum number of transactions (1-10000). Default: 1000.
        date_from (str): Keep transactions dated on or after this ISO date (YYYY-MM-DD).
        date_to (str): Keep transactions dated on or before this ISO date (YYYY-MM-DD).

    Returns:
        dict: ``{"success": True, "columns": [...], "rows": [...]}`` — one row per transaction:
        id, date, label, amount (negative = debit), currency, bank_account_id, updated_at —
        or ``{"success": False, "error": "..."}``.
    """
    columns = ["id", "date", "label", "amount", "currency", "bank_account_id", "updated_at"]
    try:
        flt = _date_filter(date_from, date_to)
        items = _fetch("/transactions", limit, {"filter": flt} if flt else None)
    except Exception as exc:  # noqa: BLE001 — mapped to a structured error dict
        return _failure(exc)
    rows = [
        {
            "id": t.get("id"),
            "date": _day(t.get("date")),
            "label": t.get("label"),
            "amount": _num(_pick(t, "currency_amount", "amount")),
            "currency": t.get("currency"),
            "bank_account_id": _party(t, "bank_account")[0],
            "updated_at": t.get("updated_at"),
        }
        for t in items
    ]
    return {"success": True, "columns": columns, "rows": rows}


@bi_source
def tool_pennylane_trial_balance(period_start: str | None = None, period_end: str | None = None, limit: int = 2000) -> dict:
    """Read the Pennylane trial balance (balance générale) as a table (read-only; also a BI data source).

    Args:
        period_start (str): First day of the period, ISO date (YYYY-MM-DD).
        period_end (str): Last day of the period, ISO date (YYYY-MM-DD).
        limit (int): Maximum number of accounts (1-10000). Default: 2000.

    Returns:
        dict: ``{"success": True, "columns": [...], "rows": [...]}`` — one row per account:
        account_number, account_label, debit, credit, balance — or
        ``{"success": False, "error": "..."}``.
    """
    columns = ["account_number", "account_label", "debit", "credit", "balance"]
    try:
        params: dict[str, Any] = {}
        for name, value in (("period_start", period_start), ("period_end", period_end)):
            if value:
                if not _DATE.match(str(value)):
                    raise ValueError(f"{name} must be an ISO date (YYYY-MM-DD), got {value!r}")
                params[name] = str(value)
        items = _fetch("/trial_balance", limit, params)
    except Exception as exc:  # noqa: BLE001 — mapped to a structured error dict
        return _failure(exc)
    rows = [
        {
            "account_number": _pick(a, "number", "account_number", "plan_item_number"),
            "account_label": _pick(a, "label", "account_label", "plan_item_label"),
            "debit": _num(_pick(a, "debits", "debit")),
            "credit": _num(_pick(a, "credits", "credit")),
            "balance": _num(a.get("balance")),
        }
        for a in items
    ]
    return {"success": True, "columns": columns, "rows": rows}

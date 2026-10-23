"""Shopify tools — read orders, order lines and products/variants as tables.

Read-only tools for BI dashboards (``@bi_source``): flat columns, numeric amounts. There
is deliberately no write tool here (no order edit, refund, cancel, fulfilment, stock
adjustment) and no customer personal data (name, e-mail, address) is returned.

API contract
------------
NOT verified against the online documentation and never called for real: paths,
parameters and field names below come from the authors' knowledge of the Shopify Admin
REST API and may need adjusting on first contact with a real shop.

* Base URL: ``https://<shop>.myshopify.com/admin/api/<version>`` (version pinned in
  ``_API_VERSION``). The domain is validated (``*.myshopify.com`` only) before any request:
  it comes from a user-editable tool_config and the access token is sent to it.
* Auth: the app access token in the ``X-Shopify-Access-Token`` header.
* ``GET /orders.json`` (``status=any``, ``limit`` up to 250, ``created_at_min`` /
  ``created_at_max``; needs ``read_orders``, plus ``read_all_orders`` beyond 60 days),
  ``GET /products.json`` (variants embedded, ``inventory_quantity``; needs ``read_products``
  and ``read_inventory``).
* Cursor pagination: the ``Link`` response header carries ``rel="next"`` with a
  ``page_info`` token; the next request sends only ``limit`` and ``page_info``. Only the
  token is taken from the link, never its host.

``SHOPIFY_SHOP_DOMAIN`` and ``SHOPIFY_ACCESS_TOKEN`` are read at module level so the
ToolsStore parameter scanner surfaces them as UI settings; both are re-read at call time
(BI sources inject the dashboard owner's tool_config through the environment).
"""

from __future__ import annotations

import os
import re
from logging import getLogger
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from apowerb.tools_store.bi_source import bi_source

logger = getLogger(__name__)

# Read at module level so the ToolsStore scanner (regex on os.getenv) discovers them.
_SHOPIFY_SHOP_DOMAIN = os.getenv("SHOPIFY_SHOP_DOMAIN", "")
_SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN", "")

_API_VERSION = "2025-01"
_HTTP_TIMEOUT_S = 30
_PAGE = 250  # documented per-page maximum
_MAX_ROWS = 20_000
_MAX_PAGES = 120  # hard stop on pagination, whatever the API answers
_SHOP = re.compile(r"^[a-z0-9][a-z0-9-]*\.myshopify\.com$")
_NEXT_LINK = re.compile(r"<([^>]+)>\s*;\s*rel=\"?next\"?")

ORDER_COLUMNS = [
    "id", "name", "created_at", "currency", "total_price", "subtotal_price", "total_discounts",
    "total_tax", "financial_status", "fulfillment_status", "channel", "country", "cancelled_at", "customer_id",
]  # fmt: skip
LINE_COLUMNS = [
    "order_id", "order_name", "created_at", "line_item_id", "product_id", "variant_id",
    "title", "variant_title", "sku", "quantity", "price", "total_discount", "line_total",
]  # fmt: skip
PRODUCT_COLUMNS = [
    "product_id", "product_title", "status", "vendor", "product_type",
    "variant_id", "variant_title", "sku", "price", "inventory_quantity", "inventory_item_id",
]  # fmt: skip


def _credentials() -> tuple[str, str]:
    """(shop host, token), read at call time; raises ValueError on a missing/invalid value."""
    token = os.environ.get("SHOPIFY_ACCESS_TOKEN") or ""
    if not token:
        raise EnvironmentError("SHOPIFY_ACCESS_TOKEN not set (Admin API access token of the custom app)")
    shop = (os.environ.get("SHOPIFY_SHOP_DOMAIN") or "").strip().lower()
    if shop and "." not in shop:
        shop = f"{shop}.myshopify.com"
    if not _SHOP.match(shop):
        raise ValueError("SHOPIFY_SHOP_DOMAIN must be the shop's myshopify.com domain (e.g. my-shop.myshopify.com)")
    return shop, token


def _num(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _failure(exc: Exception) -> dict:
    """Map an exception to the tool's error dict; the token never appears in it."""
    if isinstance(exc, (EnvironmentError, ValueError)):
        return {"success": False, "error": str(exc)}
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        detail = exc.response.text[:200].replace(os.environ.get("SHOPIFY_ACCESS_TOKEN") or "\0", "***")
        hint = {401: " (check SHOPIFY_ACCESS_TOKEN)", 403: " (missing API scope?)", 429: " (rate limited, retry later)"}.get(code, "")
        logger.warning("[SHOPIFY] HTTP %s", code)
        return {"success": False, "error": f"Shopify error (HTTP {code}){hint}. {detail}".strip()}
    logger.warning("[SHOPIFY] request failed: %s", type(exc).__name__)
    return {"success": False, "error": f"Request to Shopify failed: {type(exc).__name__}"}


def _next_page_info(resp: httpx.Response) -> str | None:
    match = _NEXT_LINK.search(resp.headers.get("Link", ""))
    if not match:
        return None
    values = parse_qs(urlparse(match.group(1)).query).get("page_info")
    return values[0] if values else None


def _fetch(resource: str, key: str, limit: int, params: dict[str, Any] | None = None) -> list[dict]:
    """Up to ``limit`` objects of ``/<resource>.json`` (GET only, cursor-paginated, bounded)."""
    wanted = max(1, min(int(limit or 1000), _MAX_ROWS))
    shop, token = _credentials()
    url = f"https://{shop}/admin/api/{_API_VERSION}/{resource}.json"
    headers = {"X-Shopify-Access-Token": token, "Accept": "application/json"}
    objects: list[dict] = []
    query: dict[str, Any] = {**(params or {}), "limit": _PAGE}
    for _ in range(_MAX_PAGES):
        resp = httpx.get(url, headers=headers, params=query, timeout=_HTTP_TIMEOUT_S)
        resp.raise_for_status()
        page = [o for o in (resp.json().get(key) or []) if isinstance(o, dict)]
        objects.extend(page)
        cursor = _next_page_info(resp)
        if len(objects) >= wanted or not page or not cursor:
            break
        query = {"limit": _PAGE, "page_info": cursor}  # Shopify rejects other filters next to page_info
    return objects[:wanted]


def _order_params(created_at_min: str | None, created_at_max: str | None, status: str) -> dict[str, Any]:
    params: dict[str, Any] = {"status": status or "any"}
    if created_at_min:
        params["created_at_min"] = created_at_min
    if created_at_max:
        params["created_at_max"] = created_at_max
    return params


def _country(order: dict) -> str | None:
    for key in ("shipping_address", "billing_address"):
        addr = order.get(key)
        if isinstance(addr, dict) and (addr.get("country_code") or addr.get("country")):
            return addr.get("country_code") or addr.get("country")
    return None


def _customer_id(order: dict) -> Any:
    customer = order.get("customer")
    return customer.get("id") if isinstance(customer, dict) else None


@bi_source
def tool_shopify_list_orders(
    limit: int = 1000, created_at_min: str | None = None, created_at_max: str | None = None, status: str = "any"
) -> dict:
    """List Shopify orders as a table (read-only; also a BI dashboard data source).

    Args:
        limit (int): Maximum number of orders (1-20000). Default: 1000.
        created_at_min (str): Only orders created at or after this ISO 8601 date/time.
        created_at_max (str): Only orders created at or before this ISO 8601 date/time.
        status (str): Order status filter (any, open, closed, cancelled). Default: any.

    Returns:
        dict: ``{"success": True, "columns": [...], "rows": [...]}`` — one row per order: id,
        name, created_at, currency, total_price, subtotal_price, total_discounts, total_tax,
        financial_status, fulfillment_status, channel (source_name), country (ISO code),
        cancelled_at, customer_id — or ``{"success": False, "error": "..."}``.
    """
    try:
        orders = _fetch("orders", "orders", limit, _order_params(created_at_min, created_at_max, status))
    except Exception as exc:  # noqa: BLE001 — mapped to a structured error dict
        return _failure(exc)
    rows = [
        {
            "id": o.get("id"),
            "name": o.get("name"),
            "created_at": o.get("created_at"),
            "currency": o.get("currency"),
            "total_price": _num(o.get("total_price")),
            "subtotal_price": _num(o.get("subtotal_price")),
            "total_discounts": _num(o.get("total_discounts")),
            "total_tax": _num(o.get("total_tax")),
            "financial_status": o.get("financial_status"),
            "fulfillment_status": o.get("fulfillment_status"),
            "channel": o.get("source_name"),
            "country": _country(o),
            "cancelled_at": o.get("cancelled_at"),
            "customer_id": _customer_id(o),
        }
        for o in orders
    ]
    return {"success": True, "columns": ORDER_COLUMNS, "rows": rows}


@bi_source
def tool_shopify_list_order_lines(
    limit: int = 1000, created_at_min: str | None = None, created_at_max: str | None = None, status: str = "any"
) -> dict:
    """List Shopify order lines (one row per line item) as a table (read-only; also a BI data source).

    Args:
        limit (int): Maximum number of ORDERS read (1-20000); each gives one row per line. Default: 1000.
        created_at_min (str): Only orders created at or after this ISO 8601 date/time.
        created_at_max (str): Only orders created at or before this ISO 8601 date/time.
        status (str): Order status filter (any, open, closed, cancelled). Default: any.

    Returns:
        dict: ``{"success": True, "columns": [...], "rows": [...]}`` — order_id, order_name,
        created_at, line_item_id, product_id, variant_id, title, variant_title, sku, quantity,
        price (unit), total_discount, line_total (price x quantity - total_discount) — or
        ``{"success": False, "error": "..."}``.
    """
    try:
        orders = _fetch("orders", "orders", limit, _order_params(created_at_min, created_at_max, status))
    except Exception as exc:  # noqa: BLE001 — mapped to a structured error dict
        return _failure(exc)
    rows = []
    for o in orders:
        for item in o.get("line_items") or []:
            qty, price, discount = _num(item.get("quantity")), _num(item.get("price")), _num(item.get("total_discount"))
            total = None if qty is None or price is None else round(price * qty - (discount or 0.0), 2)
            rows.append(
                {
                    "order_id": o.get("id"),
                    "order_name": o.get("name"),
                    "created_at": o.get("created_at"),
                    "line_item_id": item.get("id"),
                    "product_id": item.get("product_id"),
                    "variant_id": item.get("variant_id"),
                    "title": item.get("title"),
                    "variant_title": item.get("variant_title"),
                    "sku": item.get("sku"),
                    "quantity": item.get("quantity"),
                    "price": price,
                    "total_discount": discount,
                    "line_total": total,
                }
            )
    return {"success": True, "columns": LINE_COLUMNS, "rows": rows}


@bi_source
def tool_shopify_list_products(limit: int = 1000) -> dict:
    """List Shopify products with their variants and stock as a table (read-only; also a BI data source).

    Args:
        limit (int): Maximum number of PRODUCTS read (1-20000); each gives one row per variant. Default: 1000.

    Returns:
        dict: ``{"success": True, "columns": [...], "rows": [...]}`` — one row per variant:
        product_id, product_title, status, vendor, product_type, variant_id, variant_title, sku,
        price, inventory_quantity (None when stock is not tracked), inventory_item_id — or
        ``{"success": False, "error": "..."}``.
    """
    try:
        products = _fetch("products", "products", limit)
    except Exception as exc:  # noqa: BLE001 — mapped to a structured error dict
        return _failure(exc)
    rows = [
        {
            "product_id": p.get("id"),
            "product_title": p.get("title"),
            "status": p.get("status"),
            "vendor": p.get("vendor"),
            "product_type": p.get("product_type"),
            "variant_id": v.get("id"),
            "variant_title": v.get("title"),
            "sku": v.get("sku"),
            "price": _num(v.get("price")),
            "inventory_quantity": v.get("inventory_quantity"),
            "inventory_item_id": v.get("inventory_item_id"),
        }
        for p in products
        for v in (p.get("variants") or [])
        if isinstance(v, dict)
    ]
    return {"success": True, "columns": PRODUCT_COLUMNS, "rows": rows}

"""Read-only Pennylane / Shopify tools usable as dashboard sources (``source_type="tool"``).

HTTP is simulated: ``httpx.get`` is replaced by a fake that records every request and
answers from a script. No network call, no real credential (tokens below are fakes).
The field names and API paths the adapters use come from the authors' knowledge of the
vendors' public APIs and are NOT verified against the online documentation.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import httpx
import pytest

from apowerb.bi.data import tool_executor
from apowerb.bi.spec import engine, sources
from apowerb.bi.spec.models import DashboardDataRequest, MeasureSpec, SourceSpec
from apowerb.bi.spec.validate import validate_spec
from apowerb.tools_store import bi_source as bs
from apowerb.tools_store.portfolio import pennylane as pl
from apowerb.tools_store.portfolio import shopify as sh
from tests.helpers.bi_spec_data import OWNER, build_dashboard, kpi

FAKE_PL_TOKEN = "fake-pennylane-token"
FAKE_SH_TOKEN = "fake-shopify-token"
SHOP = "demo-shop.myshopify.com"

PENNYLANE_TOOLS = [
    "tool_pennylane_list_customer_invoices",
    "tool_pennylane_list_supplier_invoices",
    "tool_pennylane_list_bank_transactions",
    "tool_pennylane_trial_balance",
]
SHOPIFY_TOOLS = ["tool_shopify_list_orders", "tool_shopify_list_order_lines", "tool_shopify_list_products"]


class FakeHttp:
    """Scripted replacement for ``httpx.get``: one response per call, in order."""

    def __init__(self, *responses: httpx.Response | dict | Exception) -> None:
        self.script = list(responses)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, **kw: Any) -> httpx.Response:
        self.calls.append({"url": url, **kw})
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        if isinstance(item, dict):
            return httpx.Response(200, json=item, request=httpx.Request("GET", url))
        return item


def http_error(status: int, body: str = "") -> httpx.Response:
    return httpx.Response(status, text=body, request=httpx.Request("GET", "https://example.test"))


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("PENNYLANE_API_TOKEN", FAKE_PL_TOKEN)
    monkeypatch.setenv("SHOPIFY_ACCESS_TOKEN", FAKE_SH_TOKEN)
    monkeypatch.setenv("SHOPIFY_SHOP_DOMAIN", SHOP)
    engine.clear_caches()
    bs.reset_index()
    yield
    engine.clear_caches()
    bs.reset_index()


def customer_invoice(i: int, **over: Any) -> dict:
    base = {
        "id": 1000 + i,
        "invoice_number": f"F-2026-{i:03d}",
        "label": f"Prestation {i}",
        "date": f"2026-0{1 + i % 3}-15",
        "deadline": "2026-04-30",
        "currency": "EUR",
        "amount": "120.00",
        "currency_amount_before_tax": "100.00",
        "currency_tax": "20.00",
        "remaining_amount_with_tax": "0.00",
        "paid": True,
        "status": "paid",
        "customer": {"id": 7, "name": "ACME SAS"},
        "updated_at": "2026-03-01T10:00:00Z",
    }
    return {**base, **over}


# --------------------------------------------------------------------------- Pennylane


class TestPennylane:
    def test_customer_invoices_are_flat_typed_rows(self):
        fake = FakeHttp({"items": [customer_invoice(1), customer_invoice(2, amount="60.5", paid=False, status="late")], "has_more": False})
        with patch.object(httpx, "get", fake):
            out = pl.tool_pennylane_list_customer_invoices()
        assert out["success"] is True
        assert out["columns"] == [
            "id", "invoice_number", "label", "date", "deadline", "currency",
            "amount", "amount_before_tax", "tax", "remaining_amount", "status", "paid",
            "customer_id", "customer_name", "updated_at",
        ]  # fmt: skip
        first, second = out["rows"]
        assert first["amount"] == 120.0 and isinstance(first["amount"], float)
        assert first["amount_before_tax"] == 100.0 and first["tax"] == 20.0
        assert first["date"] == "2026-02-15" and first["customer_name"] == "ACME SAS" and first["customer_id"] == 7
        assert second["amount"] == 60.5 and second["status"] == "late" and second["paid"] is False
        assert all(set(r) == set(out["columns"]) for r in out["rows"])
        call = fake.calls[0]
        assert call["url"] == "https://app.pennylane.com/api/external/v2/customer_invoices"
        assert call["headers"]["Authorization"] == f"Bearer {FAKE_PL_TOKEN}"

    def test_supplier_invoices_use_supplier_columns(self):
        item = customer_invoice(1)
        item.pop("customer")
        item["supplier"] = {"id": 9, "name": "Fournisseur SA"}
        fake = FakeHttp({"items": [item], "has_more": False})
        with patch.object(httpx, "get", fake):
            out = pl.tool_pennylane_list_supplier_invoices()
        assert out["rows"][0]["supplier_id"] == 9 and out["rows"][0]["supplier_name"] == "Fournisseur SA"
        assert "customer_id" not in out["columns"]
        assert fake.calls[0]["url"].endswith("/supplier_invoices")

    def test_bank_transactions(self):
        tx = {"id": 5, "date": "2026-03-02", "label": "VIR ACME", "amount": "-42.10", "currency": "EUR", "bank_account": {"id": 3}}
        fake = FakeHttp({"items": [tx], "has_more": False})
        with patch.object(httpx, "get", fake):
            out = pl.tool_pennylane_list_bank_transactions()
        assert out["columns"] == ["id", "date", "label", "amount", "currency", "bank_account_id", "updated_at"]
        assert out["rows"][0]["amount"] == -42.1 and out["rows"][0]["bank_account_id"] == 3
        assert fake.calls[0]["url"].endswith("/transactions")

    def test_trial_balance_rows(self):
        items = [{"number": "411000", "label": "Clients", "debits": "300.00", "credits": "100.00", "balance": "200.00"}]
        fake = FakeHttp({"items": items, "has_more": False})
        with patch.object(httpx, "get", fake):
            out = pl.tool_pennylane_trial_balance(period_start="2026-01-01", period_end="2026-12-31")
        assert out["columns"] == ["account_number", "account_label", "debit", "credit", "balance"]
        assert out["rows"] == [{"account_number": "411000", "account_label": "Clients", "debit": 300.0, "credit": 100.0, "balance": 200.0}]
        params = fake.calls[0]["params"]
        assert params["period_start"] == "2026-01-01" and params["period_end"] == "2026-12-31"

    def test_cursor_pagination_follows_next_cursor(self):
        fake = FakeHttp(
            {"items": [customer_invoice(1), customer_invoice(2)], "has_more": True, "next_cursor": "c2"},
            {"items": [customer_invoice(3)], "has_more": False, "next_cursor": None},
        )
        with patch.object(httpx, "get", fake):
            out = pl.tool_pennylane_list_customer_invoices(limit=50)
        assert [r["id"] for r in out["rows"]] == [1001, 1002, 1003]
        assert "cursor" not in fake.calls[0]["params"]
        assert fake.calls[1]["params"]["cursor"] == "c2"

    def test_limit_bounds_rows_and_pages(self):
        page = {"items": [customer_invoice(i) for i in range(100)], "has_more": True, "next_cursor": "more"}
        fake = FakeHttp(page)  # a never-ending API
        with patch.object(httpx, "get", fake):
            out = pl.tool_pennylane_list_customer_invoices(limit=150)
        assert len(out["rows"]) == 150
        assert len(fake.calls) == 2  # stops as soon as the limit is reached

    def test_pagination_is_bounded_even_if_pages_come_back_empty_but_has_more(self):
        fake = FakeHttp({"items": [], "has_more": True, "next_cursor": "same"})
        with patch.object(httpx, "get", fake):
            out = pl.tool_pennylane_list_customer_invoices(limit=1000)
        assert out["success"] is True and out["rows"] == []
        assert len(fake.calls) <= pl._MAX_PAGES

    def test_date_filter_is_sent(self):
        fake = FakeHttp({"items": [], "has_more": False})
        with patch.object(httpx, "get", fake):
            pl.tool_pennylane_list_customer_invoices(date_from="2026-01-01", date_to="2026-03-31")
        flt = fake.calls[0]["params"]["filter"]
        assert '"gteq"' in flt and "2026-01-01" in flt and '"lteq"' in flt and "2026-03-31" in flt

    def test_bad_date_is_refused_without_http(self):
        fake = FakeHttp({"items": []})
        with patch.object(httpx, "get", fake):
            out = pl.tool_pennylane_list_customer_invoices(date_from="01/01/2026")
        assert out["success"] is False and "date_from" in out["error"]
        assert fake.calls == []

    def test_missing_token(self, monkeypatch):
        monkeypatch.delenv("PENNYLANE_API_TOKEN")
        fake = FakeHttp({"items": []})
        with patch.object(httpx, "get", fake):
            out = pl.tool_pennylane_list_customer_invoices()
        assert out["success"] is False and "PENNYLANE_API_TOKEN" in out["error"]
        assert fake.calls == []

    def test_http_error_is_a_failure_without_the_token(self):
        fake = FakeHttp(http_error(401, f"unauthorized {FAKE_PL_TOKEN}"))
        with patch.object(httpx, "get", fake):
            out = pl.tool_pennylane_list_customer_invoices()
        assert out["success"] is False and "401" in out["error"]
        assert FAKE_PL_TOKEN not in out["error"]

    def test_network_error_is_a_failure(self):
        with patch.object(httpx, "get", FakeHttp(httpx.ConnectError("boom"))):
            out = pl.tool_pennylane_list_bank_transactions()
        assert out["success"] is False and out["error"]


# --------------------------------------------------------------------------- Shopify


def order(i: int, **over: Any) -> dict:
    base = {
        "id": 5000 + i,
        "name": f"#{1000 + i}",
        "created_at": f"2026-02-0{i}T09:30:00+01:00",
        "currency": "EUR",
        "total_price": "59.90",
        "subtotal_price": "49.90",
        "total_discounts": "5.00",
        "total_tax": "10.00",
        "financial_status": "paid",
        "fulfillment_status": None,
        "source_name": "web",
        "cancelled_at": None,
        "shipping_address": {"country_code": "FR", "country": "France"},
        "customer": {"id": 77},
        "email": "client@example.com",
        "line_items": [
            {"id": 9000 + i, "product_id": 1, "variant_id": 11, "title": "Tee", "variant_title": "M", "sku": "TEE-M", "quantity": 2, "price": "20.00", "total_discount": "0.00"},
            {"id": 9100 + i, "product_id": 2, "variant_id": 22, "title": "Cap", "variant_title": None, "sku": "CAP", "quantity": 1, "price": "19.90", "total_discount": "5.00"},
        ],
    }
    return {**base, **over}


def link(page_info: str) -> dict:
    return {"Link": f'<https://{SHOP}/admin/api/2025-01/orders.json?limit=250&page_info={page_info}>; rel="next"'}


def orders_page(orders: list[dict], headers: dict | None = None) -> httpx.Response:
    return httpx.Response(200, json={"orders": orders}, headers=headers or {}, request=httpx.Request("GET", "https://x"))


class TestShopify:
    def test_orders_are_flat_rows_without_personal_data(self):
        fake = FakeHttp({"orders": [order(1), order(2, shipping_address=None, billing_address={"country_code": "BE"}, financial_status="refunded", fulfillment_status="fulfilled", source_name="pos")]})
        with patch.object(httpx, "get", fake):
            out = sh.tool_shopify_list_orders()
        assert out["success"] is True
        assert out["columns"] == [
            "id", "name", "created_at", "currency", "total_price", "subtotal_price", "total_discounts",
            "total_tax", "financial_status", "fulfillment_status", "channel", "country", "cancelled_at", "customer_id",
        ]  # fmt: skip
        a, b = out["rows"]
        assert a["total_price"] == 59.9 and a["total_discounts"] == 5.0 and a["total_tax"] == 10.0
        assert a["created_at"] == "2026-02-01T09:30:00+01:00"
        assert a["channel"] == "web" and a["country"] == "FR" and a["customer_id"] == 77
        assert b["country"] == "BE" and b["financial_status"] == "refunded" and b["fulfillment_status"] == "fulfilled"
        assert b["channel"] == "pos"
        assert "email" not in out["columns"]
        call = fake.calls[0]
        assert call["url"] == f"https://{SHOP}/admin/api/{sh._API_VERSION}/orders.json"
        assert call["headers"]["X-Shopify-Access-Token"] == FAKE_SH_TOKEN
        assert call["params"]["status"] == "any" and call["params"]["limit"] == 250

    def test_order_lines_one_row_per_line_item(self):
        fake = FakeHttp({"orders": [order(1), order(2)]})
        with patch.object(httpx, "get", fake):
            out = sh.tool_shopify_list_order_lines()
        assert out["columns"] == [
            "order_id", "order_name", "created_at", "line_item_id", "product_id", "variant_id",
            "title", "variant_title", "sku", "quantity", "price", "total_discount", "line_total",
        ]  # fmt: skip
        assert len(out["rows"]) == 4
        row = out["rows"][0]
        assert row["sku"] == "TEE-M" and row["quantity"] == 2 and row["price"] == 20.0 and row["line_total"] == 40.0
        assert row["order_id"] == 5001 and row["created_at"] == "2026-02-01T09:30:00+01:00"
        assert out["rows"][1]["line_total"] == 19.9 - 5.0 or out["rows"][1]["line_total"] == pytest.approx(14.9)

    def test_products_and_variants_with_stock(self):
        product = {
            "id": 1, "title": "Tee", "status": "active", "vendor": "Acme", "product_type": "Apparel",
            "variants": [
                {"id": 11, "title": "M", "sku": "TEE-M", "price": "20.00", "inventory_quantity": 12, "inventory_management": "shopify", "inventory_item_id": 101},
                {"id": 12, "title": "L", "sku": "TEE-L", "price": "20.00", "inventory_quantity": None, "inventory_item_id": 102},
            ],
        }
        fake = FakeHttp({"products": [product]})
        with patch.object(httpx, "get", fake):
            out = sh.tool_shopify_list_products()
        assert out["columns"] == [
            "product_id", "product_title", "status", "vendor", "product_type",
            "variant_id", "variant_title", "sku", "price", "inventory_quantity", "inventory_item_id",
        ]  # fmt: skip
        assert [r["variant_id"] for r in out["rows"]] == [11, 12]
        assert out["rows"][0]["inventory_quantity"] == 12 and out["rows"][0]["price"] == 20.0
        assert out["rows"][1]["inventory_quantity"] is None
        assert fake.calls[0]["url"].endswith("/products.json")

    def test_cursor_pagination_uses_page_info_only(self):
        fake = FakeHttp(orders_page([order(1)], link("abc123")), orders_page([order(2)]))
        with patch.object(httpx, "get", fake):
            out = sh.tool_shopify_list_orders(created_at_min="2026-01-01T00:00:00Z")
        assert [r["id"] for r in out["rows"]] == [5001, 5002]
        first, second = fake.calls
        assert first["params"]["created_at_min"] == "2026-01-01T00:00:00Z"
        # Shopify rejects filters next to page_info: only limit + page_info.
        assert second["params"] == {"limit": 250, "page_info": "abc123"}
        assert second["url"] == first["url"]

    def test_never_follows_a_foreign_link_host(self):
        evil = {"Link": '<https://evil.example/steal?page_info=zzz>; rel="next"'}
        fake = FakeHttp(orders_page([order(1)], evil), orders_page([order(2)]))
        with patch.object(httpx, "get", fake):
            out = sh.tool_shopify_list_orders()
        assert all(c["url"].startswith(f"https://{SHOP}/") for c in fake.calls)
        assert out["success"] is True

    def test_limit_and_page_cap(self):
        page = orders_page([order(1)] * 250, link("again"))
        fake = FakeHttp(page)
        with patch.object(httpx, "get", fake):
            out = sh.tool_shopify_list_orders(limit=300)
        assert len(out["rows"]) == 300 and len(fake.calls) == 2
        fake2 = FakeHttp(orders_page([], link("loop")))
        with patch.object(httpx, "get", fake2):
            assert sh.tool_shopify_list_orders(limit=5000)["success"] is True
        assert len(fake2.calls) <= sh._MAX_PAGES

    @pytest.mark.parametrize("domain", ["evil.example", "demo-shop.myshopify.com.evil.io", "http://x.myshopify.com", "a b.myshopify.com", ""])
    def test_shop_domain_is_validated_before_any_request(self, monkeypatch, domain):
        monkeypatch.setenv("SHOPIFY_SHOP_DOMAIN", domain)
        fake = FakeHttp({"orders": []})
        with patch.object(httpx, "get", fake):
            out = sh.tool_shopify_list_orders()
        assert out["success"] is False and "SHOPIFY_SHOP_DOMAIN" in out["error"]
        assert fake.calls == []
        assert FAKE_SH_TOKEN not in out["error"]

    def test_bare_shop_name_is_accepted(self, monkeypatch):
        monkeypatch.setenv("SHOPIFY_SHOP_DOMAIN", "demo-shop")
        fake = FakeHttp({"orders": []})
        with patch.object(httpx, "get", fake):
            assert sh.tool_shopify_list_orders()["success"] is True
        assert fake.calls[0]["url"].startswith("https://demo-shop.myshopify.com/")

    def test_missing_token_and_http_error(self, monkeypatch):
        with patch.object(httpx, "get", FakeHttp(http_error(401, f"bad {FAKE_SH_TOKEN}"))):
            out = sh.tool_shopify_list_products()
        assert out["success"] is False and "401" in out["error"] and FAKE_SH_TOKEN not in out["error"]
        monkeypatch.delenv("SHOPIFY_ACCESS_TOKEN")
        out = sh.tool_shopify_list_products()
        assert out["success"] is False and "SHOPIFY_ACCESS_TOKEN" in out["error"]

    def test_rate_limit_is_reported(self):
        with patch.object(httpx, "get", FakeHttp(http_error(429, "Throttled"))):
            out = sh.tool_shopify_list_orders()
        assert out["success"] is False and "429" in out["error"]


# --------------------------------------------------------------------------- allowlist


class TestAllowlist:
    @pytest.mark.parametrize("name", [f"pennylane.{t}" for t in PENNYLANE_TOOLS] + [f"shopify.{t}" for t in SHOPIFY_TOOLS] + PENNYLANE_TOOLS + SHOPIFY_TOOLS)
    def test_tools_are_allowlisted_with_tool_config_credentials(self, name):
        fn = bs.resolve_bi_source(name)
        assert bs.credentials_of(fn) == "tool_config"

    def test_every_tool_of_the_two_modules_is_a_read_source(self):
        # No tool of these modules is left outside the allowlist, and none looks like a write.
        import inspect

        bad = ("create", "write", "update", "delete", "send", "post", "upload", "remove", "set_", "pay", "refund", "cancel", "close", "fulfill")
        for module in (pl, sh):
            names = [n for n, f in inspect.getmembers(module, inspect.isfunction) if n.startswith("tool_") and f.__module__ == module.__name__]
            assert names
            assert [n for n in names if not bs.is_bi_source(getattr(module, n))] == []
            assert [n for n in names if any(w in n for w in bad)] == []

    def test_no_marked_portfolio_tool_looks_like_a_write(self):
        bad = ("create", "write", "update", "delete", "send", "post", "upload", "remove", "set_", "pay", "refund", "cancel", "fulfill")
        marked = [n for n, fn in bs._portfolio_functions().items() if bs.is_bi_source(fn) and "." in n]
        assert any(n.startswith("pennylane.") for n in marked) and any(n.startswith("shopify.") for n in marked)
        assert [n for n in marked if any(w in n.split(".", 1)[1] for w in bad)] == []

    def test_modules_have_no_non_get_http_call(self):
        import inspect

        for module in (pl, sh):
            src = inspect.getsource(module)
            for verb in ("httpx.post", "httpx.put", "httpx.patch", "httpx.delete", '"POST"', '"PUT"', '"PATCH"', '"DELETE"'):
                assert verb not in src, (module.__name__, verb)

    def test_credentials_are_discovered_for_the_ui(self):
        from apowerb.tools_store.tool_manager import get_tools_store

        store = get_tools_store()
        keys = {p["key"] for p in store.get_tool_expected_params("pennylane.x")}
        assert "PENNYLANE_API_TOKEN" in keys
        keys = {p["key"] for p in store.get_tool_expected_params("shopify.x")}
        assert {"SHOPIFY_ACCESS_TOKEN", "SHOPIFY_SHOP_DOMAIN"} <= keys


# --------------------------------------------------------------------------- end to end


def tool_source(tool: str, args: dict | None = None, cfg: str | None = "tool_config1", **kw: Any) -> SourceSpec:
    options: dict[str, Any] = {"tool": tool}
    if args is not None:
        options["args"] = args
    return SourceSpec(name="ventes", kind="connector", source_type="tool", source_options=options, connection_config_id=cfg, **kw)


class TestEndToEnd:
    async def test_pennylane_invoices_to_a_dashboard_kpi(self, monkeypatch):
        # The ambient env must not matter: the owner's tool_config is what the tool sees.
        monkeypatch.delenv("PENNYLANE_API_TOKEN")
        fake = FakeHttp({"items": [customer_invoice(1), customer_invoice(2, amount="80.00"), customer_invoice(3, amount="50.00")], "has_more": False})
        seen = {}

        def fake_load(config_id, owner_id):
            seen.update(config_id=config_id, owner_id=owner_id)
            return "pennylane.x", {"PENNYLANE_API_TOKEN": FAKE_PL_TOKEN}

        source = tool_source("pennylane.tool_pennylane_list_customer_invoices", {"limit": 100}).model_copy(
            update={"measures": [MeasureSpec(name="ca_ttc", label="CA TTC", expr="SUM(amount)", format="integer")]}
        )
        dashboard = build_dashboard({"k": kpi("ca_ttc")}, source=source, filters=[])
        assert validate_spec(dashboard) == []
        with patch.object(tool_executor, "load_tool_config_params", fake_load), patch.object(httpx, "get", fake):
            result = await engine.compute_dashboard(dashboard, DashboardDataRequest(filters={}), owner=OWNER)
        assert result.components["k"].error is None, result.components["k"].error
        assert result.components["k"].value.value == 250
        assert seen == {"config_id": "tool_config1", "owner_id": OWNER}
        assert fake.calls[0]["headers"]["Authorization"] == f"Bearer {FAKE_PL_TOKEN}"
        import os

        assert "PENNYLANE_API_TOKEN" not in os.environ

    async def test_shopify_orders_to_a_dashboard_kpi(self, monkeypatch):
        monkeypatch.delenv("SHOPIFY_ACCESS_TOKEN")
        monkeypatch.delenv("SHOPIFY_SHOP_DOMAIN")
        fake = FakeHttp({"orders": [order(1), order(2, total_price="40.10")]})
        params = {"SHOPIFY_ACCESS_TOKEN": FAKE_SH_TOKEN, "SHOPIFY_SHOP_DOMAIN": SHOP}
        source = tool_source("shopify.tool_shopify_list_orders").model_copy(
            update={"measures": [MeasureSpec(name="ca", label="CA", expr="SUM(total_price)", format="integer")]}
        )
        dashboard = build_dashboard({"k": kpi("ca")}, source=source, filters=[])
        with patch.object(tool_executor, "load_tool_config_params", lambda c, owner_id: ("shopify.x", params)), patch.object(httpx, "get", fake):
            result = await engine.compute_dashboard(dashboard, DashboardDataRequest(filters={}), owner=OWNER)
        assert result.components["k"].error is None, result.components["k"].error
        assert result.components["k"].value.value == 100
        assert fake.calls[0]["url"].startswith(f"https://{SHOP}/")

    async def test_tool_failure_reaches_the_author_without_the_token(self):
        source = tool_source("pennylane.tool_pennylane_list_customer_invoices")
        with patch.object(tool_executor, "load_tool_config_params", lambda c, owner_id: ("p", {"PENNYLANE_API_TOKEN": FAKE_PL_TOKEN})):
            with patch.object(httpx, "get", FakeHttp(http_error(403, FAKE_PL_TOKEN))):
                with pytest.raises(Exception) as exc:
                    await sources.load_source(source, OWNER)
        assert "403" in str(exc.value) and FAKE_PL_TOKEN not in str(exc.value)

    async def test_frame_columns_are_normalised(self):
        source = tool_source("shopify.tool_shopify_list_products")
        product = {"id": 1, "title": "Tee", "variants": [{"id": 11, "sku": "S", "price": "2.0", "inventory_quantity": 3}]}
        with patch.object(tool_executor, "load_tool_config_params", lambda c, owner_id: ("s", {"SHOPIFY_ACCESS_TOKEN": FAKE_SH_TOKEN, "SHOPIFY_SHOP_DOMAIN": SHOP})):
            with patch.object(httpx, "get", FakeHttp({"products": [product]})):
                df = await sources.load_source(source, OWNER)
        assert df["inventory_quantity"].tolist() == [3] and df["price"].tolist() == [2.0]

"""Dashboard spec v2: ``source_type="tool"`` connector sources.

An allowlisted, read-only agent tool is run as the dashboard owner and its
tabular answer (a list of dicts, or ``{"rows": [...]}``) becomes a source.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import patch

import pytest

from apowerb.bi.data import tool_executor
from apowerb.bi.spec import engine, sources
from apowerb.bi.spec.errors import SpecError
from apowerb.bi.spec.models import DashboardDataRequest, MeasureSpec, SourceSpec
from apowerb.bi.spec.validate import validate_spec
from apowerb.core.invocation_context import resolve_integration_user
from apowerb.tools_store import bi_source as bs
from tests.helpers.bi_spec_data import OWNER, build_dashboard, kpi

CALLS: list[dict[str, Any]] = []


@pytest.fixture(autouse=True)
def _clean():
    engine.clear_caches()
    CALLS.clear()
    saved = dict(bs._REGISTRY)
    yield
    bs._REGISTRY.clear()
    bs._REGISTRY.update(saved)
    engine.clear_caches()


def tool_source(tool: str = "fake_read", args: dict | None = None, cfg: str | None = None, **opts: Any) -> SourceSpec:
    options: dict[str, Any] = {"tool": tool, **opts}
    if args is not None:
        options["args"] = args
    return SourceSpec(name="ventes", kind="connector", source_type="tool", source_options=options, connection_config_id=cfg)


def register(fn, credentials: str = "integration"):
    bs.register_bi_source(fn, credentials=credentials)
    return fn


def fake_read(region: str = "all") -> list[dict]:
    CALLS.append({"tool": "fake_read", "region": region, "owner": resolve_integration_user()})
    return [{"Mois": "2026-01-01", "Ventes": 10, "Region": region}, {"Mois": "2026-02-01", "Ventes": 12, "Region": region}]


class TestToolSource:
    async def test_list_of_dicts_becomes_a_normalised_frame(self):
        register(fake_read)
        df = await sources.load_source(tool_source(args={"region": "nord"}), OWNER)
        assert list(df.columns) == ["mois", "ventes", "region"]
        assert df["ventes"].tolist() == [10, 12]
        assert set(df["region"]) == {"nord"}

    async def test_rows_envelope_is_accepted(self):
        def fake_env() -> dict:
            return {"success": True, "columns": ["a", "b"], "rows": [{"a": 1, "b": 2}, {"a": 3, "b": 4}]}

        register(fake_env)
        df = await sources.load_source(tool_source("fake_env"), OWNER)
        assert df["a"].tolist() == [1, 3]

    async def test_nested_values_are_flattened_to_text(self):
        def fake_nested() -> list[dict]:
            return [{"id": 1, "tags": ["a", "b"], "meta": {"k": 1}}]

        register(fake_nested)
        df = await sources.load_source(tool_source("fake_nested"), OWNER)
        assert df["tags"].tolist() == ['["a", "b"]']

    async def test_tool_is_run_as_the_dashboard_owner(self):
        register(fake_read)
        await sources.load_source(tool_source(), OWNER)
        assert CALLS[0]["owner"] == OWNER
        # the identity is bound to the call only, never left on the caller
        assert resolve_integration_user() != OWNER

    async def test_tool_not_on_the_allowlist_is_refused(self):
        def fake_write(x: str = "") -> dict:
            CALLS.append({"tool": "fake_write"})
            return {"success": True, "rows": [{"a": 1}]}

        with patch.object(bs, "_portfolio_functions", lambda: {"fake_write": fake_write}):
            with pytest.raises(SpecError, match="fake_write"):
                await sources.load_source(tool_source("fake_write"), OWNER)
        assert CALLS == []

    async def test_unknown_tool_gives_the_same_refusal(self):
        with patch.object(bs, "_portfolio_functions", lambda: {}):
            with pytest.raises(SpecError, match="liste des outils"):
                await sources.load_source(tool_source("nope"), OWNER)

    async def test_text_answer_is_an_error(self):
        def fake_text() -> str:
            return "Voici vos ventes : 10, 12"

        register(fake_text)
        with pytest.raises(SpecError, match="tabulaire"):
            await sources.load_source(tool_source("fake_text"), OWNER)

    async def test_dict_without_rows_is_an_error(self):
        def fake_norows() -> dict:
            return {"success": True, "records": [{"a": 1}]}

        register(fake_norows)
        with pytest.raises(SpecError, match="rows"):
            await sources.load_source(tool_source("fake_norows"), OWNER)

    @pytest.mark.parametrize(
        "answer",
        [
            {"success": False, "error": "Odoo integration is incomplete."},
            {"status": "error", "error_message": "Odoo integration is incomplete."},
            {"status": "error", "message": "Odoo integration is incomplete.", "rows": []},
        ],
    )
    async def test_failure_answers_carry_the_tool_message(self, answer):
        def fake_fail() -> dict:
            return answer

        register(fake_fail)
        with pytest.raises(SpecError, match="Odoo integration is incomplete"):
            await sources.load_source(tool_source("fake_fail"), OWNER)

    async def test_tool_exception_is_a_generic_source_error(self):
        def fake_boom() -> list:
            raise RuntimeError("secret-token-123")

        register(fake_boom)
        with pytest.raises(SpecError) as exc:
            await sources.load_source(tool_source("fake_boom"), OWNER)
        assert "secret-token-123" not in str(exc.value)

    async def test_bad_arguments_are_reported(self):
        register(fake_read)
        with pytest.raises(SpecError, match="arguments"):
            await sources.load_source(tool_source(args={"inconnu": 1}), OWNER)

    async def test_row_cap(self):
        def fake_big() -> list[dict]:
            return [{"a": i} for i in range(6)]

        register(fake_big)
        with patch.object(sources, "MAX_SOURCE_ROWS", 5):
            with pytest.raises(SpecError, match="trop de lignes"):
                await sources.load_source(tool_source("fake_big"), OWNER)

    async def test_async_tool_is_awaited(self):
        async def fake_async() -> list[dict]:
            return [{"a": 1}]

        register(fake_async)
        df = await sources.load_source(tool_source("fake_async"), OWNER)
        assert df["a"].tolist() == [1]


class TestToolConfigCredentials:
    """Env-var tools get the owner's tool_config for the duration of the call only."""

    def make(self):
        def fake_env_read() -> list[dict]:
            CALLS.append({"token": os.environ.get("FAKE_TOKEN")})
            return [{"a": 1}]

        return register(fake_env_read, credentials="tool_config")

    async def test_config_is_required(self):
        self.make()
        with pytest.raises(SpecError, match="connection_config_id"):
            await sources.load_source(tool_source("fake_env_read"), OWNER)

    async def test_params_are_loaded_for_the_owner_and_restored(self):
        self.make()
        os.environ["FAKE_TOKEN"] = "ambient-other-tenant"
        try:
            seen: dict[str, Any] = {}

            def fake_load(config_id, owner_id):
                seen.update(config_id=config_id, owner_id=owner_id)
                return "fake.tool", {"FAKE_TOKEN": "owner-token"}

            with patch.object(tool_executor, "load_tool_config_params", fake_load):
                await sources.load_source(tool_source("fake_env_read", cfg="tool_config7"), OWNER)
            assert seen == {"config_id": "tool_config7", "owner_id": OWNER}
            assert CALLS[0]["token"] == "owner-token"
            assert os.environ["FAKE_TOKEN"] == "ambient-other-tenant"
        finally:
            os.environ.pop("FAKE_TOKEN", None)

    async def test_missing_config_is_an_error(self):
        self.make()
        with patch.object(tool_executor, "load_tool_config_params", lambda c, owner_id: (None, None)):
            with pytest.raises(SpecError, match="introuvable"):
                await sources.load_source(tool_source("fake_env_read", cfg="tool_config9"), OWNER)
        assert CALLS == []


class TestToolValidation:
    def errors_for(self, source: SourceSpec) -> list[str]:
        return validate_spec(build_dashboard({}, source=source))

    def test_options_without_tool_are_rejected(self):
        source = SourceSpec(name="ventes", kind="connector", source_type="tool", source_options={"args": {}})
        assert any("source_options.tool" in e for e in self.errors_for(source))

    def test_args_must_be_an_object(self):
        register(fake_read)
        assert any("args" in e for e in self.errors_for(tool_source(args=["x"])))  # type: ignore[arg-type]

    def test_tool_must_be_allowlisted(self):
        with patch.object(bs, "_portfolio_functions", lambda: {}):
            assert any("liste des outils" in e for e in self.errors_for(tool_source("inconnu")))

    def test_tool_config_tools_need_a_config(self):
        register(fake_read, credentials="tool_config")
        assert any("connection_config_id" in e for e in self.errors_for(tool_source()))

    def test_valid_source_has_no_error(self):
        register(fake_read)
        assert self.errors_for(tool_source(args={"region": "nord"})) == []


class TestEngine:
    async def test_measures_and_cache_work_on_a_tool_source(self):
        register(fake_read)
        source = tool_source(args={"region": "nord"}).model_copy(
            update={"measures": [MeasureSpec(name="total", label="Total", expr="SUM(ventes)", format="integer")]}
        )
        dashboard = build_dashboard({"k": kpi("total")}, source=source, filters=[])
        first = await engine.compute_dashboard(dashboard, DashboardDataRequest(filters={}), owner=OWNER)
        assert first.components["k"].error is None, first.components["k"].error
        assert first.components["k"].value.value == 22
        await engine.compute_dashboard(dashboard, DashboardDataRequest(filters={}), owner=OWNER)
        assert len(CALLS) == 1


class TestPortfolioAllowlist:
    @pytest.fixture(autouse=True)
    def _fresh_index(self):
        bs.reset_index()
        yield
        bs.reset_index()

    @pytest.mark.parametrize(
        "name",
        [
            "google_sheets.tool_read_sheet_table",
            "tool_read_sheet_table",
            "hubspot.tool_hubspot_list_deals",
            "odoo.tool_odoo_search_table",
        ],
    )
    def test_reading_tools_are_allowlisted(self, name):
        assert bs.is_bi_source(bs.resolve_bi_source(name))

    @pytest.mark.parametrize(
        "name",
        [
            "google_sheets.tool_write_cells",
            "tool_write_cells",
            "tool_read_sheet",  # raw 2D values: not tabular
            "hubspot.tool_hubspot_create_deal",
            "hubspot.tool_hubspot_create_contact",
            "odoo.tool_odoo_create_record",
            "odoo.tool_odoo_update_record",
            "google_gmail.tool_send_email",
            "no_such.tool",
        ],
    )
    def test_writing_and_untabular_tools_are_refused(self, name):
        with pytest.raises(bs.BiSourceError):
            bs.resolve_bi_source(name)

    def test_no_marked_portfolio_tool_looks_like_a_write(self):
        bad = ("create", "write", "update", "delete", "send", "post", "upload", "remove", "set_")
        marked = [n for n, fn in bs._portfolio_functions().items() if bs.is_bi_source(fn) and "." in n]
        assert marked
        assert [n for n in marked if any(w in n.split(".", 1)[1] for w in bad)] == []


class TestPortfolioAdapters:
    def test_sheet_header_row_becomes_columns(self):
        from apowerb.tools_store.portfolio import google_sheets as gs

        values = [["Mois", "Ventes", "", "Ventes"], ["jan", "10", "x"], ["fév", "12", "", "7"]]
        with patch.object(gs, "_read_values", lambda *a, **k: {"status": "ok", "values": values}):
            out = gs.tool_read_sheet_table("sid")
        assert out["success"] is True
        assert out["columns"] == ["Mois", "Ventes", "colonne_3", "Ventes_2"]
        assert out["rows"][0] == {"Mois": "jan", "Ventes": "10", "colonne_3": "x", "Ventes_2": None}
        assert out["rows"][1]["colonne_3"] is None

    def test_sheet_error_is_reported_as_a_failure(self):
        from apowerb.tools_store.portfolio import google_sheets as gs

        with patch.object(gs, "_read_values", lambda *a, **k: {"status": "error", "message": "not connected"}):
            assert gs.tool_read_sheet_table("sid") == {"success": False, "error": "not connected"}

    def test_hubspot_deals_are_flattened_and_paged(self):
        from apowerb.tools_store.portfolio import hubspot as hs

        pages = [
            {"results": [{"id": "1", "properties": {"dealname": "A", "amount": "10"}}], "paging": {"next": {"after": "2"}}},
            {"results": [{"id": "2", "properties": {"dealname": "B", "amount": "20"}}]},
        ]
        paths: list[str] = []

        def fake_request(method, path, **kw):
            paths.append(path)
            return pages[len(paths) - 1]

        with patch.object(hs, "_request", fake_request):
            out = hs.tool_hubspot_list_deals(properties=["dealname", "amount"], limit=50)
        assert out["success"] is True
        assert [r["id"] for r in out["rows"]] == ["1", "2"]
        assert out["rows"][1] == {"id": "2", "dealname": "B", "amount": "20"}
        assert paths[0].startswith("/crm/v3/objects/deals?limit=50&properties=dealname,amount")
        assert "after=2" in paths[1]

    def test_hubspot_failure_carries_the_message(self):
        from apowerb.tools_store.portfolio import hubspot as hs

        def boom(*a, **k):
            raise EnvironmentError("HUBSPOT_ACCESS_TOKEN not set")

        with patch.object(hs, "_request", boom):
            out = hs.tool_hubspot_list_deals()
        assert out == {"success": False, "error": "HUBSPOT_ACCESS_TOKEN not set"}

    def test_odoo_table_names_many2one_and_requires_fields(self):
        from apowerb.tools_store.portfolio import odoo as od

        recs = [{"id": 5, "name": "SO001", "partner_id": [7, "ACME"], "amount_total": 99.5}]
        with patch.object(od, "tool_odoo_search_records", lambda *a, **k: {"success": True, "records": recs}):
            out = od.tool_odoo_search_table("sale.order", ["name", "partner_id", "amount_total"])
        assert out["rows"] == [{"id": 5, "name": "SO001", "partner_id": "ACME", "amount_total": 99.5}]
        assert out["columns"] == ["id", "name", "partner_id", "amount_total"]
        assert od.tool_odoo_search_table("sale.order", [])["success"] is False

    def test_odoo_failure_carries_the_message(self):
        from apowerb.tools_store.portfolio import odoo as od

        with patch.object(od, "tool_odoo_search_records", lambda *a, **k: {"success": False, "error": "incomplete"}):
            assert od.tool_odoo_search_table("res.partner", ["name"]) == {"success": False, "error": "incomplete"}

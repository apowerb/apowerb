"""Catalogue : version, origine, colonnes de stock reel ; provenance portee par le tableau."""

from __future__ import annotations

from apowerb.bi.dashboards.core import Dashboard
from apowerb.bi.dashboards.schema import DashboardResponse
from apowerb.bi.spec import catalog

COLUMNS = {
    "date": "semaine", "family": "famille", "sales": "ventes", "site": None,
    "stock": None, "safety": None, "lead_time": None,
}


def test_builtin_descriptor_carries_version_origin_and_stock_columns():
    described = catalog.get_template("stock_pilotage").describe()
    assert described["version"] == catalog.STOCK_PILOTAGE_VERSION == 1
    assert (described["origin"], described["owner"], described["visibility"], described["can_delete"]) == (
        "builtin", None, None, False,
    )
    by_key = {c["key"]: c for c in described["columns"]}
    assert list(by_key) == ["date", "family", "sales", "site", "stock", "safety", "lead_time"]
    assert {k: (c["required"], c["default"]) for k, c in by_key.items()} == {
        "date": (True, "semaine"), "family": (True, "famille"), "sales": (True, "ventes"),
        "site": (False, None), "stock": (False, "stock"),
        "safety": (False, "seuil_securite"), "lead_time": (False, "delai_fournisseur_sem"),
    }


def test_optional_columns_left_blank_stay_none():
    template = catalog.get_template("stock_pilotage")
    clean, errors = template.clean_columns({"date": "semaine", "family": "famille", "sales": "ventes"})
    assert errors == []
    assert clean["stock"] is None and clean["safety"] is None and clean["lead_time"] is None


def _built(columns):
    template = catalog.get_template("stock_pilotage")
    clean, _ = template.clean_columns(columns)
    return template.build("ds-1", clean, {"horizon": 8})


def test_without_stock_the_spec_is_simulated():
    spec = _built({**COLUMNS})
    assert "plan" not in [s["name"] for s in spec["sources"]]


def test_with_stock_the_spec_plans_the_orders_from_the_real_stock():
    spec = _built({**COLUMNS, "stock": "stock", "safety": "seuil_securite", "lead_time": "delai_fournisseur_sem"})
    assert [s["name"] for s in spec["sources"]][:3] == ["stocks", "plan", "horizon"]
    titles = [c["visual"].get("title") for c in spec["components"]]
    assert "Stock actuel" in titles
    plan = next(s for s in spec["sources"] if s["name"] == "plan")
    step = plan["transforms"][-1]
    assert (step["stock"], step["safety_column"], step["lead_time_column"]) == (
        "stock", "seuil_securite", "delai_fournisseur_sem",
    )


def test_dashboard_template_provenance_round_trips():
    ref = {"id": "stock_pilotage", "version": 1, "columns": {"date": "semaine", "site": None}, "params": {"horizon": 8}}
    dashboard = Dashboard.create("T", template=ref)
    assert dashboard.template.model_dump() == ref
    assert DashboardResponse.from_domain(dashboard).template.model_dump() == ref
    assert Dashboard(**dashboard.model_dump(mode="json")).template.model_dump() == ref
    assert DashboardResponse.from_domain(Dashboard.create("U")).template is None

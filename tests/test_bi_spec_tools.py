"""Outils agent BI v2 (bi_dashboard_tools) : création, patch, requête, description,
cloisonnement par propriétaire, JSON invalide. Moteur et base sont simulés."""
import contextlib
import json
import sys
import types

import pytest

from apowerb.bi.spec.models import (
    ComponentResult,
    DashboardDataResponse,
    KpiValue,
    ResolvedFilter,
    SourceMeta,
    FieldSpec,
)
import apowerb.tools_store.portfolio.bi_dashboard_tools as t

ALICE = "alice@example.com"
BOB = "bob@example.com"


class SpecError(Exception):
    pass


class FakeDb:
    """Tous les propriétaires partagent la table ; chaque store ne voit que les siens."""

    def __init__(self):
        self.rows = {}  # id -> (owner, dashboard)


class FakeStore:
    def __init__(self, db, owner):
        self.db, self.owner = db, owner

    async def get(self, dashboard_id):
        row = self.db.rows.get(dashboard_id)
        return row[1] if row and row[0].lower() == self.owner.lower() else None

    async def save(self, dashboard):
        self.db.rows[dashboard.id] = (self.owner, dashboard)
        return dashboard


@pytest.fixture
def env(monkeypatch):
    db = FakeDb()
    computed = []

    @contextlib.asynccontextmanager
    async def store(owner):
        yield FakeStore(db, owner)

    async def fake_compute(dashboard, filters, owner, components=None):
        computed.append({"filters": filters, "owner": owner, "components": components})
        results = {}
        for c in dashboard.components:
            v = c.visual
            if v.title == "Cassé":
                results[c.id] = ComponentResult(type=v.type, error="colonne inconnue: x")
            elif v.type == "kpi":
                results[c.id] = ComponentResult(
                    type="kpi",
                    value=KpiValue(value=0.987, formatted="98,7 %", status="warn", secondary="x", target_label="cible"),
                )
            elif v.type == "text":
                results[c.id] = ComponentResult(type="text", text="Toutes familles : stock de 1 200 unités")
            elif v.type == "note":
                results[c.id] = ComponentResult(type="note", text="note")
            else:
                results[c.id] = ComponentResult(
                    type=v.type, rows=[{"x": i, "v": i / 3, "_color_v": "good"} for i in range(500)]
                )
        return DashboardDataResponse(
            dashboard_id=dashboard.id,
            filters={"famille": ResolvedFilter(value=None, label="Toutes familles")},
            filter_options={"famille": [f"F{i}" for i in range(100)]},
            sources={"stocks": SourceMeta(fields=[FieldSpec(name="ventes", label="Ventes")], row_count=10)},
            components=results,
        )

    def fake_apply(dashboard, ops):
        data = dashboard.model_dump()
        for op in ops:
            if op.op == "set_title":
                data["title"] = op.title
            elif op.op == "remove_component":
                if op.component_id not in {c.id for c in dashboard.components}:
                    raise SpecError(f"component '{op.component_id}' not found")
                data["components"] = [c for c in data["components"] if c["id"] != op.component_id]
            else:
                raise SpecError(f"unsupported op {op.op}")
        return type(dashboard).model_validate(data)

    errors = types.ModuleType("apowerb.bi.spec.errors")
    errors.SpecError = SpecError
    monkeypatch.setitem(sys.modules, "apowerb.bi.spec.errors", errors)
    monkeypatch.setattr(t, "_dashboard_store", store)
    monkeypatch.setattr(t, "_compute", fake_compute)
    monkeypatch.setattr(t, "_apply_ops", fake_apply)
    monkeypatch.setattr(t, "_validate", lambda d: [])
    monkeypatch.setattr(t, "_agent_owner", lambda: ALICE)
    return types.SimpleNamespace(db=db, computed=computed, monkeypatch=monkeypatch)


SPEC = {
    "sources": [{
        "name": "stocks", "kind": "dataset", "dataset_id": "ds-1",
        "measures": [{"name": "total", "label": "Total", "expr": "SUM(ventes)"}],
    }],
    "filters": [],
    "components": [
        {"visual": {"type": "kpi", "title": "Total", "source": "stocks", "kpi": {"measure": "total"}},
         "position": {"row": 0, "col": 0, "width": 3, "height": 2}},
        {"visual": {"type": "bar", "title": "Ventes", "source": "stocks", "x": "semaine", "series": [{"key": "total"}]},
         "position": {"row": 2, "col": 0, "width": 12, "height": 4}},
        {"visual": {"type": "hbar", "title": "Cassé", "source": "stocks", "x": "famille", "series": [{"key": "total"}]},
         "position": {"row": 6, "col": 0, "width": 12, "height": 4}},
    ],
}


def _create(title="Test", spec=SPEC):
    return t.tool_bi_create_dashboard(title, json.dumps(spec))


def test_create_returns_id_url_values_and_component_errors(env):
    res = _create()
    assert res["success"] is True
    assert res["url"] == f"/bi/{res['dashboard_id']}"
    kpi = next(c for c in res["components"] if c["type"] == "kpi")
    assert kpi["value"] == "98,7 %" and kpi["status"] == "warn"
    assert list(res["errors"].values()) == ["Cassé: colonne inconnue: x"]
    saved = env.db.rows[res["dashboard_id"]]
    assert saved[0] == ALICE and saved[1].spec_version == 2 and len(saved[1].sources) == 1
    assert env.computed[0]["owner"] == ALICE


def test_create_output_is_compact(env):
    res = _create()
    chart = next(c for c in res["components"] if c["type"] == "bar")
    assert chart["row_count"] == 500 and "rows" not in chart and len(chart["sample"]) == 2
    assert "_color_v" not in chart["sample"][0]


def test_create_rejects_invalid_spec_without_saving(env):
    env.monkeypatch.setattr(t, "_validate", lambda d: ["mesure 'x' inconnue sur 'stocks'"])
    res = _create()
    assert res["success"] is False and res["issues"] == ["mesure 'x' inconnue sur 'stocks'"]
    assert env.db.rows == {}


@pytest.mark.parametrize("bad", ["{not json", "[1, 2]", '{"sources": []}', '{"foo": 1}',
                                 '{"sources": [{"name": "Bad Name", "kind": "dataset"}]}',
                                 '{"sources": [{"name": "s", "kind": "dataset"}], "components": [{"position": {}}]}'])
def test_create_invalid_json_gives_actionable_message(env, bad):
    res = t.tool_bi_create_dashboard("T", bad)
    assert res["success"] is False and len(res["error"]) > 20
    assert env.db.rows == {}


def test_create_needs_title_and_owner(env):
    assert "title" in t.tool_bi_create_dashboard(" ", json.dumps(SPEC))["error"]
    env.monkeypatch.setattr(t, "_agent_owner", lambda: "")
    assert "owner" in _create()["error"].lower()


def test_compute_failure_does_not_lose_the_dashboard(env):
    async def boom(*a, **k):
        raise RuntimeError("moteur indisponible")

    env.monkeypatch.setattr(t, "_compute", boom)
    res = _create()
    assert res["success"] is True and "moteur indisponible" in res["preview_error"]
    assert res["dashboard_id"] in env.db.rows


def test_create_stock_dashboard(env):
    res = t.tool_bi_create_stock_dashboard("ds-9", site="site", default_period="13w")
    assert res["success"] is True and res["component_count"] == 19
    dash = env.db.rows[res["dashboard_id"]][1]
    assert dash.sources[0].dataset_id == "ds-9"
    assert dash.sources[0].transforms[0].op == "aggregate"
    assert dash.filters[1].default == "13w"
    assert any(c["type"] == "text" for c in res["components"])


def test_create_stock_dashboard_with_real_stock(env):
    res = t.tool_bi_create_stock_dashboard(
        "ds-9", stock="stock", safety_column="seuil_securite", lead_time_column="delai_fournisseur_sem"
    )
    assert res["success"] is True and res["component_count"] == 12
    dash = env.db.rows[res["dashboard_id"]][1]
    assert [s.name for s in dash.sources] == ["stocks", "plan", "horizon"]
    plan = dash.sources[1].transforms[-1]
    assert plan.op == "replenishment_plan" and plan.stock == "stock"
    assert plan.safety_column == "seuil_securite" and plan.lead_time_column == "delai_fournisseur_sem"
    assert plan.target_cover == 4 and plan.horizon == 8


def test_create_stock_dashboard_records_the_template_provenance(env):
    res = t.tool_bi_create_stock_dashboard("ds-9", site="site", horizon=12)
    ref = env.db.rows[res["dashboard_id"]][1].template
    assert ref.model_dump() == {
        "id": "stock_pilotage",
        "version": 1,
        "columns": {"date": "semaine", "family": "famille", "sales": "ventes", "site": "site",
                    "stock": None, "safety": None, "lead_time": None},
        "params": {"horizon": 12},
    }


def test_create_stock_dashboard_with_real_stock_records_the_stock_columns(env):
    res = t.tool_bi_create_stock_dashboard(
        "ds-9", stock="stock", safety_column="seuil_securite", lead_time_column="delai_fournisseur_sem"
    )
    columns = env.db.rows[res["dashboard_id"]][1].template.columns
    assert (columns["stock"], columns["safety"], columns["lead_time"], columns["site"]) == (
        "stock", "seuil_securite", "delai_fournisseur_sem", None,
    )


def test_free_form_dashboard_has_no_template_provenance(env):
    assert env.db.rows[_create()["dashboard_id"]][1].template is None


def test_create_stock_dashboard_bad_column(env):
    res = t.tool_bi_create_stock_dashboard("ds-9", date="Semaine")
    assert res["success"] is False and "not a valid column name" in res["error"]


def test_patch_ok_and_saved(env):
    dash_id = _create()["dashboard_id"]
    res = t.tool_bi_patch_dashboard(dash_id, json.dumps([{"op": "set_title", "title": "Nouveau"}]))
    assert res["success"] is True and res["applied_ops"] == 1
    assert env.db.rows[dash_id][1].title == "Nouveau"


def test_patch_dry_run_does_not_save(env):
    dash_id = _create()["dashboard_id"]
    res = t.tool_bi_patch_dashboard(dash_id, '{"ops": [{"op": "set_title", "title": "Brouillon"}]}', dry_run=True)
    assert res["success"] is True and res["dry_run"] is True
    assert env.db.rows[dash_id][1].title == "Test"


def test_patch_readable_errors(env):
    dash_id = _create()["dashboard_id"]
    refused = t.tool_bi_patch_dashboard(dash_id, json.dumps([{"op": "remove_component", "component_id": "nope"}]))
    assert refused["success"] is False and "nothing was changed" in refused["error"] and "nope" in refused["error"]
    unknown = t.tool_bi_patch_dashboard(dash_id, json.dumps([{"op": "explode"}]))
    assert unknown["success"] is False and "Invalid operations" in unknown["error"]
    assert t.tool_bi_patch_dashboard(dash_id, "[]")["success"] is False
    assert "not valid JSON" in t.tool_bi_patch_dashboard(dash_id, "[{")["error"]
    assert env.db.rows[dash_id][1].title == "Test"


def test_patch_invalid_result_is_not_saved(env):
    dash_id = _create()["dashboard_id"]
    env.monkeypatch.setattr(t, "_validate", lambda d: ["titre vide"])
    res = t.tool_bi_patch_dashboard(dash_id, json.dumps([{"op": "set_title", "title": "X"}]))
    assert res["success"] is False and res["issues"] == ["titre vide"]
    assert env.db.rows[dash_id][1].title == "Test"


def test_query_returns_values_with_truncated_rows(env):
    dash_id = _create()["dashboard_id"]
    res = t.tool_bi_query_dashboard(dash_id, json.dumps({"famille": "A"}))
    assert res["success"] is True
    assert env.computed[-1]["filters"] == {"famille": "A"}
    chart = next(c for c in res["components"] if c["type"] == "bar")
    assert len(chart["rows"]) == 30 and chart["truncated"] is True and chart["row_count"] == 500
    assert len(res["filter_options"]["famille"]) == 20
    assert res["filters"]["famille"]["label"] == "Toutes familles"
    assert "not valid JSON" in t.tool_bi_query_dashboard(dash_id, "{oops")["error"]
    assert "object" in t.tool_bi_query_dashboard(dash_id, "[1]")["error"]


PERIOD_SPEC = {**SPEC, "filters": [
    {"id": "famille", "label": "Famille", "field": "famille", "type": "select"},
    {"id": "periode", "label": "Periode", "field": "semaine", "type": "relative_period",
     "default": "26w", "presets": ["13w", "26w", "all"]},
    {"id": "dates", "label": "Dates", "field": "semaine", "type": "date_range"},
]}


def test_group_by_a_period_filter_runs_once_per_preset(env):
    """L'agent compare souvent les périodes : group_by="periode" le faisait
    échouer (seuls les filtres select avaient des valeurs) et il réessayait."""
    dash_id = _create(spec=PERIOD_SPEC)["dashboard_id"]
    res = t.tool_bi_query_dashboard(dash_id, json.dumps({"famille": "A"}), component_ids="Total", group_by="periode")
    assert res["success"] is True, res
    assert [g["value"] for g in res["groups"]] == ["13w", "26w", "all"]
    assert [c["filters"] for c in env.computed[-3:]] == [
        {"famille": "A", "periode": p} for p in ("13w", "26w", "all")]


def test_group_by_error_lists_the_filters_it_accepts(env):
    dash_id = _create(spec=PERIOD_SPEC)["dashboard_id"]
    res = t.tool_bi_query_dashboard(dash_id, component_ids="Total", group_by="dates")
    assert res["success"] is False
    assert "famille, periode" in res["error"] and "dates" not in res["error"].split(":", 1)[1]


def test_describe(env):
    dash_id = _create()["dashboard_id"]
    res = t.tool_bi_describe_dashboard(dash_id)
    assert res["success"] is True and res["spec_version"] == 2
    src = res["sources"][0]
    assert src["name"] == "stocks" and src["measures"][0] == {"name": "total", "label": "Total", "expr": "SUM(ventes)", "format": "number"}
    assert src["fields"][0]["name"] == "ventes"  # inferred fields come from the engine
    assert {c["type"] for c in res["components"]} == {"kpi", "bar", "hbar"}
    assert all("id" in c and "position" in c for c in res["components"])


def test_describe_survives_engine_failure(env):
    dash_id = _create()["dashboard_id"]

    async def boom(*a, **k):
        raise RuntimeError("down")

    env.monkeypatch.setattr(t, "_compute", boom)
    res = t.tool_bi_describe_dashboard(dash_id)
    assert res["success"] is True and res["sources"][0]["fields"] == []


@pytest.mark.parametrize("call", [
    lambda i: t.tool_bi_describe_dashboard(i),
    lambda i: t.tool_bi_query_dashboard(i),
    lambda i: t.tool_bi_patch_dashboard(i, json.dumps([{"op": "set_title", "title": "Vol"}])),
])
def test_other_owners_dashboard_is_invisible(env, call):
    dash_id = _create()["dashboard_id"]
    env.monkeypatch.setattr(t, "_agent_owner", lambda: BOB)
    res = call(dash_id)
    assert res["success"] is False and "not found" in res["error"]
    assert env.db.rows[dash_id][1].title == "Test"


def test_unknown_id(env):
    assert "not found" in t.tool_bi_describe_dashboard("zzz")["error"]


def test_tools_are_discovered_and_registered():
    from apowerb.tools_store.tool_manager import ToolsStore

    names = {
        "tool_bi_create_dashboard", "tool_bi_create_stock_dashboard", "tool_bi_patch_dashboard",
        "tool_bi_query_dashboard", "tool_bi_describe_dashboard",
    }
    tools = set(ToolsStore().get_tools_in_category("bi_dashboard_tools"))
    assert {f"bi_dashboard_tools.{n}" for n in names} <= tools

    from apowerb.core.superagents.templates.dashboard import DASHBOARD_TEMPLATES

    # Data Analyst keeps the chart-based dashboard family only
    # (test_text_to_sql_template.test_one_dashboard_family_only).
    assert {f"bi_dashboard_tools.{n}" for n in names} <= set(DASHBOARD_TEMPLATES[0]["recommended_tools"])


def test_list_dashboard_templates_shows_otif_and_quality_with_their_columns():
    res = t.tool_bi_list_dashboard_templates()
    assert res["success"] is True
    by_id = {tpl["id"]: tpl for tpl in res["templates"]}
    assert {"stock_pilotage", "delivery_otif", "production_quality"} <= set(by_id)
    assert any("required" in line for line in by_id["delivery_otif"]["columns"].values())


@pytest.mark.parametrize(
    "template_id",
    [
        "delivery_otif",
        "production_quality",
        "sales_tracking",
        "sales_forecast",
        "receivables_aging",
        "purchasing_suppliers",
        "support_sla",
    ],
)
def test_create_dashboard_from_template_with_renamed_columns(env, template_id):
    from apowerb.bi.spec.catalog import get_template

    template = get_template(template_id)
    columns = {c.key: f"col_{c.key}" for c in template.columns if c.required}
    res = t.tool_bi_create_dashboard_from_template(template_id, "ds-7", columns_json=json.dumps(columns))
    assert res["success"] is True, res
    dash = env.db.rows[res["dashboard_id"]][1]
    assert dash.title == template.label
    assert dash.sources[0].dataset_id == "ds-7"
    assert dash.template.id == template_id and dash.template.version == template.version
    assert {k: v for k, v in dash.template.columns.items() if v} == columns


def test_create_dashboard_from_template_uses_default_columns(env):
    res = t.tool_bi_create_dashboard_from_template("delivery_otif", "ds-7", title="OTIF Nord")
    assert res["success"] is True, res
    assert env.db.rows[res["dashboard_id"]][1].title == "OTIF Nord"


def test_create_dashboard_from_template_rejects_unknown_template_and_columns(env):
    res = t.tool_bi_create_dashboard_from_template("nope", "ds-7")
    assert res["success"] is False and "delivery_otif" in res["error"]
    res = t.tool_bi_create_dashboard_from_template("delivery_otif", "ds-7", columns_json='{"zzz": "a"}')
    assert res["success"] is False and "zzz" in res["error"]
    res = t.tool_bi_create_dashboard_from_template("delivery_otif", "ds-7", columns_json="[1]")
    assert res["success"] is False
    assert env.db.rows == {}


def test_template_tool_does_not_assume_optional_columns(env):
    # stock_pilotage declares optional defaults (stock, seuil_securite...): a dataset without
    # them must give the simulated dashboard, not a real-stock one on missing columns.
    res = t.tool_bi_create_dashboard_from_template("stock_pilotage", "ds-7")
    assert res["success"] is True, res
    dash = env.db.rows[res["dashboard_id"]][1]
    assert [s.name for s in dash.sources] == ["stocks", "prevision_future"]
    assert dash.template.columns["stock"] is None and dash.template.columns["safety"] is None
    res = t.tool_bi_create_dashboard_from_template("stock_pilotage", "ds-7", columns_json='{"stock": "stock"}')
    assert [s.name for s in env.db.rows[res["dashboard_id"]][1].sources] == ["stocks", "plan", "horizon"]


def test_template_tools_speak_the_requested_language(env):
    listed = t.tool_bi_list_dashboard_templates(language="en")
    labels = {tpl["id"]: tpl["label"] for tpl in listed["templates"]}
    assert labels["delivery_otif"] == "Deliveries / OTIF"
    assert t.tool_bi_list_dashboard_templates()["templates"][0]["label"] == "Pilotage stocks"
    res = t.tool_bi_create_dashboard_from_template("delivery_otif", "ds-7", language="en")
    assert res["success"] is True, res
    dash = env.db.rows[res["dashboard_id"]][1]
    assert dash.title == "Deliveries / OTIF" and dash.locale == "en"
    assert t.tool_bi_create_dashboard_from_template("delivery_otif", "ds-7", language="de")["success"] is False
    french = t.tool_bi_create_dashboard_from_template("production_quality", "ds-7")
    assert env.db.rows[french["dashboard_id"]][1].locale is None


def test_stock_tool_speaks_the_requested_language(env):
    res = t.tool_bi_create_stock_dashboard("ds-9", title="", language="en")
    assert res["success"] is True, res
    dash = env.db.rows[res["dashboard_id"]][1]
    assert dash.title == "Inventory steering" and dash.locale == "en"
    assert dash.filters[0].label == "Product family"


# ---------------------------------------------------------------------------
# Taille des réponses : chaque caractère renvoyé est relu à chaque appel suivant
# du même tour (mesuré en DEV le 15/10 : la liste des gabarits ~6k tokens,
# une requête de tableau ~4,6k tokens, relus 3 à 4 fois).
# ---------------------------------------------------------------------------
def test_list_dashboard_templates_is_compact_but_keeps_every_mapping_fact():
    from apowerb.bi.spec.catalog import list_templates

    res = t.tool_bi_list_dashboard_templates("en")
    full = [tpl.describe() for tpl in list_templates("en")]
    by_id = {tpl["id"]: tpl for tpl in res["templates"]}
    assert set(by_id) == {tpl["id"] for tpl in full}
    for tpl in full:
        entry = by_id[tpl["id"]]
        assert entry["label"] == tpl["label"] and entry["description"] == tpl["description"]
        assert list(entry["columns"]) == [c["key"] for c in tpl["columns"]]
        for col in tpl["columns"]:
            text = entry["columns"][col["key"]]
            assert col["type"] in text and col["label"] in text
            assert ("required" if col["required"] else "optional") in text
            if col["default"]:
                assert f"default column: {col['default']}" in text
        for p in tpl["params"]:
            text = entry["params"][p["key"]]
            assert all(str(p[k]) in text for k in ("type", "default", "min", "max"))
    # 23 620 -> 16 339 caractères, 7 004 -> 4 196 tokens Gemini (15/10).
    assert len(json.dumps(res["templates"])) < 0.7 * len(json.dumps(full))


def test_query_rows_are_sent_as_columns_and_rows(env):
    dash_id = _create()["dashboard_id"]
    chart = next(c for c in t.tool_bi_query_dashboard(dash_id)["components"] if c["type"] == "bar")
    assert chart["columns"] == ["x", "v"]  # _color_* reste dehors
    assert chart["rows"][3] == [3, round(3 / 3, 4)]
    assert len(chart["rows"]) == 30 and chart["truncated"] is True and chart["row_count"] == 500


def test_query_can_be_limited_to_some_components(env):
    created = _create()
    kpi_id = next(c["id"] for c in created["components"] if c["type"] == "kpi")
    res = t.tool_bi_query_dashboard(created["dashboard_id"], '{"famille": "A"}', component_ids=f" {kpi_id} ,Ventes")
    assert res["success"] is True
    assert [c["title"] for c in res["components"]] == ["Total", "Ventes"]
    assert set(env.computed[-1]["components"]) == {kpi_id, next(c["id"] for c in created["components"] if c.get("title") == "Ventes")}


def test_query_with_unknown_component_names_the_valid_ones(env):
    dash_id = _create()["dashboard_id"]
    res = t.tool_bi_query_dashboard(dash_id, component_ids="nope")
    assert res["success"] is False
    assert "nope" in res["error"] and "Total" in res["error"]

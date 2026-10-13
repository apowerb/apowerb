"""Gabarit « Prévision de trésorerie » (module templates_cash, contrat DESCRIPTOR/build) :
spec valide, grille, calcul de bout en bout avec le moteur de prévision simulé."""
import asyncio

import pandas as pd
import pytest

from apowerb.bi.dashboards.core import ComponentType, Dashboard, DashboardComponent, GridPosition
from apowerb.bi.spec import templates_cash
from apowerb.bi.spec.catalog import DashboardTemplate, TemplateColumn, TemplateParam
from apowerb.bi.spec.models import DashboardDataRequest, FilterSpec, SourceSpec, VisualSpec
from apowerb.bi.spec.templates_cash import DESCRIPTOR, build
from apowerb.bi.spec.validate import validate_spec

COLS = {"date": "semaine", "inflow": "encaissements", "outflow": "decaissements", "balance": "solde",
        "account": "compte", "threshold": None}
LEVEL = {("encaissements", "A"): 300, ("encaissements", "B"): 500,
         ("decaissements", "A"): 400, ("decaissements", "B"): 400}


def _dashboard(spec: dict) -> Dashboard:
    return Dashboard.create(
        "Trésorerie",
        sources=[SourceSpec.model_validate(s) for s in spec["sources"]],
        filters=[FilterSpec.model_validate(f) for f in spec["filters"]],
        components=[
            DashboardComponent(
                component_type=ComponentType.VISUAL,
                position=GridPosition(**c["position"]),
                visual=VisualSpec.model_validate(c["visual"]),
            )
            for c in spec["components"]
        ],
        spec_version=2,
    )


def _data(with_balance=True, with_account=True):
    start = pd.Timestamp("2023-01-02")
    rows = []
    for acc, solde_end in (("A", 1000.0), ("B", 5000.0)):
        for w in range(104):
            row = {"semaine": start + pd.Timedelta(weeks=w), "compte": acc,
                   "encaissements": float(LEVEL[("encaissements", acc)]),
                   "decaissements": float(LEVEL[("decaissements", acc)]),
                   "solde": solde_end if w == 103 else 3000.0}
            rows.append(row)
    df = pd.DataFrame(rows)
    if not with_balance:
        df = df.drop(columns=["solde"])
    if not with_account:
        df = df[df["compte"] == "A"].drop(columns=["compte"])
    return df


def _fake_engine(body):
    last = max(pd.Timestamp(r["semaine"]) for r in body["data"])
    target = body["target_var"]
    groups = sorted({r["compte"] for r in body["data"]}) if body["group_var"] else [None]
    return {"series": [
        {"group": g, "forecast": [{"date": (last + pd.Timedelta(weeks=k)).strftime("%Y-%m-%d"),
                                   "value": LEVEL[(target, g or "A")]} for k in range(1, body["horizon"] + 1)]}
        for g in groups
    ]}


@pytest.fixture
def computed(monkeypatch):
    import apowerb.bi.spec.sources as sources
    import apowerb.bi.spec.transforms as transforms
    from apowerb.bi.spec.engine import compute_dashboard

    def run(columns=None, params=None, engine=_fake_engine, **data_kw):
        async def fake_load(dataset_id, owner, sheet=None):
            return _data(**data_kw)

        transforms.clear_cache()
        monkeypatch.setattr(sources, "load_dataset", fake_load)
        monkeypatch.setattr(transforms, "_call_forecast_engine", engine)
        spec = build("ds-1", {**COLS, **(columns or {})}, {"horizon": 8, "min_cash": 700, **(params or {})})
        dash = _dashboard(spec)
        return dash, asyncio.run(compute_dashboard(dash, DashboardDataRequest(), owner="alice@example.com"))

    return run


def _kpis(dash, resp):
    return {c.visual.kpi.measure: resp.components[c.id].value.value for c in dash.components if c.visual.type == "kpi"}


# ---------------------------------------------------------------------------
# Contrat de module
# ---------------------------------------------------------------------------


def test_descriptor_au_contrat_du_catalogue():
    assert DESCRIPTOR["id"] == "cash_forecast" and DESCRIPTOR["version"] == 1
    assert DESCRIPTOR["label"] and DESCRIPTOR["description"]
    keys = {c["key"] for c in DESCRIPTOR["columns"]}
    assert keys == {"date", "inflow", "outflow", "balance", "account", "threshold"}
    required = {c["key"] for c in DESCRIPTOR["columns"] if c["required"]}
    assert required == {"date", "inflow", "outflow"}
    assert {p["key"] for p in DESCRIPTOR["params"]} >= {"horizon", "inflow_shock_pct", "outflow_shock_pct", "min_cash"}
    template = DashboardTemplate(
        id=DESCRIPTOR["id"], label=DESCRIPTOR["label"], description=DESCRIPTOR["description"],
        columns=tuple(TemplateColumn(c["key"], c["label"], c["type"], c["required"], c.get("default"))
                      for c in DESCRIPTOR["columns"]),
        params=tuple(TemplateParam(p["key"], p["label"], p["type"], p["default"], p["min"], p["max"])
                     for p in DESCRIPTOR["params"]),
        build=build,
    )
    clean, errors = template.clean_params({})
    assert errors == [] and clean["inflow_shock_pct"] == 10 and clean["outflow_shock_pct"] == 10
    assert template.clean_params({"inflow_shock_pct": 80})[1]
    cols, errors = template.clean_columns({"date": "semaine", "inflow": "enc", "outflow": "dec"})
    assert errors == [] and cols["balance"] is None
    assert template.clean_columns({"date": "semaine"})[1]
    spec = template.build("ds-1", cols, clean)
    assert spec["sources"][0]["kind"] == "dataset" and spec["sources"][0]["dataset_id"] == "ds-1"


def test_colonne_obligatoire_manquante():
    with pytest.raises(ValueError, match="Encaissements"):
        build("ds-1", {"date": "semaine", "outflow": "decaissements"}, {})


@pytest.mark.parametrize("columns", [{"inflow": "Encaissements"}, {"account": "mon compte"}, {"balance": "a b"}])
def test_noms_de_colonnes_invalides(columns):
    with pytest.raises(ValueError, match="not a valid column name"):
        build("ds-1", {**COLS, **columns}, {})


def test_parametres_invalides_refuses():
    with pytest.raises(ValueError):
        templates_cash.cash_forecast("ds-1", inflow_shock=1.5)


# ---------------------------------------------------------------------------
# Spec
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("columns", [{}, {"account": None}, {"balance": None, "account": None, "threshold": "seuil"}])
@pytest.mark.parametrize("params", [{}, {"monthly": 1, "horizon": 12}])
def test_spec_valide_et_sans_chevauchement(columns, params):
    spec = build("ds-1", {**COLS, **columns}, params)
    assert validate_spec(_dashboard(spec)) == []
    assert [s["name"] for s in spec["sources"]] == ["tresorerie", "plan", "horizon"]
    ops = [t["op"] for t in spec["sources"][1]["transforms"]]
    assert ops == ["forecast", "forecast", "cash_forecast"]
    taken = set()
    for comp in spec["components"]:
        p = comp["position"]
        assert p["col"] + p["width"] <= 12
        cells = {(r, c) for r in range(p["row"], p["row"] + p["height"]) for c in range(p["col"], p["col"] + p["width"])}
        assert not cells & taken
        taken |= cells


def test_validation_connait_les_colonnes_de_sortie_de_l_etape():
    from apowerb.bi.spec.models import CashForecastStep
    from apowerb.bi.spec.validate import _CASH_OUTPUTS, _schema_of

    source = SourceSpec(name="plan", kind="dataset", dataset_id="ds-1",
                        transforms=[CashForecastStep(date="semaine", forecast_inflow="enc_prev")])
    schema = _schema_of(source, {"plan": source}, {}, set())
    assert set(_CASH_OUTPUTS) <= schema.columns and {"enc_prev", "decaissements_prevus"} <= schema.columns
    assert "semaine_plan" in _CASH_OUTPUTS and "seuil_tresorerie" in _CASH_OUTPUTS


def test_graphique_principal_trois_scenarios_et_seuil():
    spec = build("ds-1", COLS, {})
    combo = next(c["visual"] for c in spec["components"] if c["visual"]["type"] == "combo" and
                 c["visual"]["source"] == "plan")
    assert [s["key"] for s in combo["series"]] == [
        "solde_central_plan", "solde_pessimiste_plan", "solde_optimiste_plan", "seuil_total"]
    assert combo["x"] == "semaine"


def test_etape_cash_forecast_reprend_les_parametres():
    spec = build("ds-1", {**COLS, "threshold": "seuil"},
                 {"horizon": 6, "inflow_shock_pct": 20, "outflow_shock_pct": 5, "min_cash": 1000, "monthly": 1})
    step = spec["sources"][1]["transforms"][-1]
    assert step["inflow_shock"] == 0.2 and step["outflow_shock"] == 0.05 and step["horizon"] == 6
    assert step["min_cash"] == 1000 and step["min_cash_column"] == "seuil" and step["by"] == ["compte"]
    assert spec["sources"][1]["transforms"][0]["frequency"] == "month"
    assert "frequency" not in build("ds-1", COLS, {})["sources"][1]["transforms"][0]


# ---------------------------------------------------------------------------
# Bout en bout (moteur de prévision simulé)
# ---------------------------------------------------------------------------


def test_tableau_de_bord_calcule_de_bout_en_bout(computed):
    dash, resp = computed()
    broken = {c.visual.title: resp.components[c.id].error for c in dash.components if resp.components[c.id].error}
    assert broken == {}
    kpis = _kpis(dash, resp)
    assert kpis["solde_actuel"] == 6000
    # A : 1000 - 100 x 8 = 200 ; B : 5000 + 100 x 8 = 5800
    assert kpis["solde_horizon_central"] == pytest.approx(6000)
    # A : 1000 - 170 x 8 ; B : 5000 + 10 x 8
    assert kpis["semaines_tresorerie"] == pytest.approx(6000 / 800)
    # seuil 700 par compte : A passe sous 700 en 4e semaine (centrale)
    assert kpis["premiere_sous_seuil"] == 4
    assert not resp.sources["plan"].warnings
    table = next(c for c in dash.components if c.visual.type == "table")
    rows = {r["compte"]: r for r in resp.components[table.id].rows if not r.get("_total")}
    assert set(rows) == {"A", "B"}
    assert rows["A"]["solde_horizon_pessimiste"] == pytest.approx(-360)
    assert rows["A"]["solde_horizon_optimiste"] == pytest.approx(760)
    assert rows["B"]["solde_horizon_pessimiste"] == pytest.approx(5080)
    assert rows["B"]["premiere_sous_seuil"] is None
    total = next(r for r in resp.components[table.id].rows if r.get("_total"))
    assert total["solde_actuel"] == 6000 and total["premiere_sous_seuil"] == 4
    # Flux prévus : seulement les périodes de l'horizon.
    flux = next(c for c in dash.components if (c.visual.title or "").startswith("Flux prévus"))
    pts = resp.components[flux.id].rows
    assert len(pts) == 8
    assert min(pd.Timestamp(r["semaine"]) for r in pts) > pd.Timestamp("2023-01-02") + pd.Timedelta(weeks=103)
    assert pts[0]["encaissements_prevus_m"] == 800 and pts[0]["decaissements_prevus_m"] == 800


def test_graphique_principal_seuil_sur_toute_la_largeur_et_scenarios(computed):
    dash, resp = computed()
    combo = next(c for c in dash.components if c.visual.type == "combo" and c.visual.source == "plan")
    pts = sorted(resp.components[combo.id].rows, key=lambda r: pd.Timestamp(r["semaine"]))
    assert len(pts) == 26 + 8
    assert all(p["seuil_total"] == 1400 for p in pts)  # 2 comptes x 700, historique compris
    assert pts[25]["solde_pessimiste_plan"] == pts[25]["solde_optimiste_plan"] == 6000
    assert pts[-1]["solde_pessimiste_plan"] == pytest.approx(-360 + 5080)
    assert pts[-1]["solde_optimiste_plan"] == pytest.approx(760 + 6520)


def test_sans_compte_ni_solde_le_solde_part_du_solde_d_ouverture(computed):
    dash, resp = computed(columns={"account": None, "balance": None}, params={"opening_balance": 1000},
                          with_balance=False, with_account=False)
    broken = {c.visual.title: resp.components[c.id].error for c in dash.components if resp.components[c.id].error}
    assert broken == {}
    kpis = _kpis(dash, resp)
    assert kpis["solde_actuel"] == pytest.approx(1000 + 104 * (300 - 400))
    assert kpis["solde_horizon_central"] == pytest.approx(1000 - 104 * 100 - 800)


def test_moteur_indisponible_flux_en_repli_avec_avertissement(computed):
    from apowerb.bi.spec.transforms import ForecastUnavailable

    def down(body):
        raise ForecastUnavailable("service de prévision indisponible")

    dash, resp = computed(engine=down)
    broken = {c.visual.title: resp.components[c.id].error for c in dash.components if resp.components[c.id].error}
    assert broken == {}
    kpis = _kpis(dash, resp)
    assert kpis["solde_horizon_central"] == pytest.approx(6000)
    assert any("moyenne des 4 dernières périodes" in w for w in resp.sources["plan"].warnings)

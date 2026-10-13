"""Gabarit « Restauration » : une ligne par jour (ou service), calcul de bout en bout."""
import asyncio

import pandas as pd
import pytest

from apowerb.bi.dashboards.core import ComponentType, Dashboard, DashboardComponent, GridPosition
from apowerb.bi.spec import engine
from apowerb.bi.spec.models import DashboardDataRequest, FilterSpec, SourceSpec, VisualSpec
from apowerb.bi.spec.templates_restaurant import DESCRIPTOR, build
from apowerb.bi.spec.validate import validate_spec

REQUIRED = dict(date="date", revenue="ca", covers="couverts", food_cost="achats_matieres",
                labor_cost="masse_salariale")
FULL = {**REQUIRED, "service": "service"}


def _dashboard(spec: dict) -> Dashboard:
    return Dashboard.create(
        "Restauration",
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


def _days() -> pd.DataFrame:
    """Semaine 1 (lun 01/01) : lundi, mardi en 2 services, samedi ; semaine 2 : lundi, samedi."""
    rows = [
        # date, ca, couverts, achats, masse salariale, service
        ("2024-01-01", 1000, 40, 300, 350, "midi"),   # lundi
        ("2024-01-02", 300, 12, 90, 150, "midi"),     # mardi, midi
        ("2024-01-02", 500, 20, 150, 200, "soir"),    # mardi, soir
        ("2024-01-06", 2000, 80, 700, 400, "soir"),   # samedi
        ("2024-01-08", 1200, 48, 360, 350, "midi"),   # lundi
        ("2024-01-13", 2600, 100, 800, 450, "soir"),  # samedi
    ]
    df = pd.DataFrame(rows, columns=["date", "ca", "couverts", "achats_matieres", "masse_salariale", "service"])
    df["date"] = pd.to_datetime(df["date"])
    return df


@pytest.fixture
def compute(monkeypatch):
    import apowerb.bi.spec.sources as sources
    import apowerb.bi.spec.transforms as transforms

    def run(spec, frame=None, filters=None):
        async def fake_load(dataset_id, owner, sheet=None):
            return (_days() if frame is None else frame).copy()

        engine.clear_caches()
        transforms.clear_cache()
        monkeypatch.setattr(sources, "load_dataset", fake_load)
        dash = _dashboard(spec)
        resp = asyncio.run(engine.compute_dashboard(
            dash, DashboardDataRequest(filters=filters or {}), owner="alice@example.com"))
        return dash, resp

    return run


def _kpis(dash, resp):
    return {c.visual.kpi.measure: resp.components[c.id].value for c in dash.components if c.visual.type == "kpi"}


def _by_title(dash, resp, title):
    comp = next(c for c in dash.components if c.visual.title == title)
    return resp.components[comp.id]


def _errors(dash, resp):
    return {c.visual.title: resp.components[c.id].error for c in dash.components if resp.components[c.id].error}


def _titles(dash):
    return {c.visual.title for c in dash.components if c.visual.title}


# ---------------------------------------------------------------- descripteur


def test_descriptor_conforme():
    assert DESCRIPTOR["id"] == "restaurant" and DESCRIPTOR["version"] == 1
    assert DESCRIPTOR["label"] and DESCRIPTOR["description"]
    cols = {c["key"]: c for c in DESCRIPTOR["columns"]}
    assert {k for k, c in cols.items() if c["required"]} == set(REQUIRED)
    assert set(cols) == set(FULL)
    for c in cols.values():
        assert set(c) == {"key", "label", "type", "required", "default"}
        assert c["type"] in ("date", "string", "number")
    assert [cols[k]["default"] for k in REQUIRED] == ["date", "ca", "couverts", "achats_matieres", "masse_salariale"]
    assert cols["service"]["type"] == "string" and cols["date"]["type"] == "date"
    params = {p["key"]: p for p in DESCRIPTOR["params"]}
    assert {k: p["default"] for k, p in params.items()} == {
        "food_cost_min": 25, "food_cost_max": 35, "prime_cost_min": 55, "prime_cost_max": 65}
    for p in params.values():
        assert set(p) == {"key", "label", "type", "default", "min", "max"}
        assert p["min"] <= p["default"] <= p["max"]


# ---------------------------------------------------------------- structure


@pytest.mark.parametrize("columns", [FULL, REQUIRED])
def test_spec_valide_et_sans_chevauchement(columns):
    spec = build("ds-1", columns, {})
    assert validate_spec(_dashboard(spec)) == []
    assert spec["sources"][0]["kind"] == "dataset" and spec["sources"][0]["dataset_id"] == "ds-1"
    taken = set()
    for comp in spec["components"]:
        p = comp["position"]
        assert p["col"] >= 0 and p["col"] + p["width"] <= 12
        cells = {(r, c) for r in range(p["row"], p["row"] + p["height"]) for c in range(p["col"], p["col"] + p["width"])}
        assert not cells & taken
        taken |= cells


@pytest.mark.parametrize("missing", list(REQUIRED))
@pytest.mark.parametrize("blank", [None, ""])
def test_colonne_obligatoire_manquante(missing, blank):
    with pytest.raises(ValueError):
        build("ds-1", {**FULL, missing: blank}, {})


def test_dataset_obligatoire_et_noms_valides():
    with pytest.raises(ValueError):
        build("", FULL, {})
    with pytest.raises(ValueError, match="not a valid column name"):
        build("ds-1", {**FULL, "revenue": "CA HT"}, {})
    with pytest.raises(ValueError, match="not a valid column name"):
        build("ds-1", {**FULL, "service": "service'"}, {})


@pytest.mark.parametrize("params", [
    {"food_cost_min": 40, "food_cost_max": 30},   # min >= max
    {"prime_cost_min": 65, "prime_cost_max": 65},
    {"food_cost_max": 0},
    {"food_cost_max": 150},
    {"prime_cost_min": 1.5},
    {"food_cost_min": True},
])
def test_parametres_invalides(params):
    with pytest.raises(ValueError):
        build("ds-1", FULL, params)


def test_bornes_reglables_dans_les_lignes_de_reference():
    spec = build("ds-1", FULL, {"food_cost_min": 20, "food_cost_max": 30, "prime_cost_min": 50, "prime_cost_max": 60})
    charts = {c["visual"].get("title"): c["visual"] for c in spec["components"]}
    assert [r["value"] for r in charts["Coût matière et personnel par semaine"]["reference_lines"]] == [0.2, 0.3]
    assert [r["value"] for r in charts["Prime cost par semaine"]["reference_lines"]] == [0.5, 0.6]
    measures = {m["name"]: m for m in spec["sources"][0]["measures"]}
    assert measures["cout_matiere"]["thresholds"]["good"] == pytest.approx(0.30)
    assert measures["prime_cost"]["thresholds"]["good"] == pytest.approx(0.60)


# ---------------------------------------------------------------- calcul


def test_calcul_de_bout_en_bout(compute):
    dash, resp = compute(build("ds-1", FULL, {}))
    assert _errors(dash, resp) == {}
    k = _kpis(dash, resp)
    # CA 7 600, couverts 300, achats 2 400, masse salariale 1 900
    assert k["ca"].value == 7600
    assert k["cout_matiere"].value == pytest.approx(2400 / 7600)
    assert k["cout_personnel"].value == pytest.approx(1900 / 7600)
    assert k["prime_cost"].value == pytest.approx(4300 / 7600)
    assert k["ticket_moyen"].value == pytest.approx(7600 / 300)
    assert k["cout_matiere"].status == "good" and k["prime_cost"].status == "good"

    ratios = _by_title(dash, resp, "Coût matière et personnel par semaine")
    assert [r["cout_matiere"] for r in ratios.rows] == [pytest.approx(1240 / 3800), pytest.approx(1160 / 3800)]
    assert [r["cout_personnel"] for r in ratios.rows] == [pytest.approx(1100 / 3800), pytest.approx(800 / 3800)]
    assert [ln["value"] for ln in ratios.reference_lines] == [0.25, 0.35]
    prime = _by_title(dash, resp, "Prime cost par semaine")
    assert [r["prime_cost"] for r in prime.rows] == [pytest.approx(2340 / 3800), pytest.approx(1960 / 3800)]
    assert [ln["value"] for ln in prime.reference_lines] == [0.55, 0.65]

    ca_cov = _by_title(dash, resp, "CA et couverts par semaine")
    assert [(r["ca"], r["couverts"]) for r in ca_cov.rows] == [(3800, 152), (3800, 148)]

    # CA moyen par jour ouvert : le mardi compte un seul jour malgré ses deux services
    weekday = {r["x"]: r for r in _by_title(dash, resp, "CA moyen par jour de semaine").rows}
    assert list(weekday) == ["1-Lun", "2-Mar", "6-Sam"]
    assert weekday["1-Lun"]["ca_moyen_jour"] == pytest.approx(1100)
    assert weekday["2-Mar"]["ca_moyen_jour"] == pytest.approx(800)
    assert weekday["6-Sam"]["ca_moyen_jour"] == pytest.approx(2300)
    assert weekday["6-Sam"]["ca"] == 4600

    service = {r["x"]: r["value"] for r in _by_title(dash, resp, "CA par service").rows}
    assert service == {"midi": 2500, "soir": 5100}
    text = next(resp.components[c.id].text for c in dash.components if c.visual.type == "text")
    assert "7" in text


def test_les_bornes_changent_le_statut(compute):
    # matière 31,6 % : conforme à 35 %, à surveiller si la cible monte à 30 % (tolérance de 5 points)
    dash, resp = compute(build("ds-1", FULL, {"food_cost_max": 30}))
    assert _kpis(dash, resp)["cout_matiere"].status == "warn"
    dash, resp = compute(build("ds-1", FULL, {"food_cost_max": 25, "food_cost_min": 10}))
    assert _kpis(dash, resp)["cout_matiere"].status == "bad"


def test_filtre_service(compute):
    dash, resp = compute(build("ds-1", FULL, {}), filters={"service": "midi"})
    k = _kpis(dash, resp)
    assert k["ca"].value == 1000 + 300 + 1200
    assert k["ticket_moyen"].value == pytest.approx(2500 / (40 + 12 + 48))


# ---------------------------------------------------------------- colonne optionnelle absente


def test_sans_service(compute):
    spec = build("ds-1", REQUIRED, {})
    dash, resp = compute(spec)
    assert _errors(dash, resp) == {}
    assert "CA par service" not in _titles(dash)
    assert [f.id for f in dash.filters] == ["periode"]
    assert _kpis(dash, resp)["prime_cost"].value == pytest.approx(4300 / 7600)
    assert "CA moyen par jour de semaine" in _titles(dash)


def test_division_par_zero_sans_ca(compute):
    df = _days()
    df["ca"] = 0
    df["couverts"] = 0
    dash, resp = compute(build("ds-1", FULL, {}), frame=df)
    assert _errors(dash, resp) == {}
    k = _kpis(dash, resp)
    assert k["prime_cost"].value is None and k["ticket_moyen"].value is None

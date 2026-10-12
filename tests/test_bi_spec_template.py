"""Gabarit « Pilotage stocks » : spec valide, positions sans chevauchement,
mesures référencées existantes, avec et sans colonne site."""
import importlib.util
import re

import pytest

from apowerb.bi.dashboards.core import ComponentType, Dashboard, DashboardComponent, GridPosition
from apowerb.bi.spec.models import FilterSpec, SourceSpec, VisualSpec
from apowerb.bi.spec.templates import stock_pilotage

HAS_VALIDATE = importlib.util.find_spec("apowerb.bi.spec.validate") is not None


def _dashboard(spec: dict) -> Dashboard:
    return Dashboard.create(
        "Pilotage",
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


@pytest.fixture(params=[None, "site"], ids=["sans_site", "avec_site"])
def spec(request):
    return stock_pilotage("ds-1", site=request.param)


def test_spec_round_trip_pydantic(spec):
    dash = _dashboard(spec)
    assert dash.spec_version == 2
    assert len(dash.components) == 19
    assert [s.name for s in dash.sources] == ["stocks", "prevision_future"]
    assert [f.id for f in dash.filters] == ["famille", "periode"]


def test_site_ajoute_une_agregation_en_tete():
    sans = stock_pilotage("ds-1")["sources"][0]["transforms"]
    avec = stock_pilotage("ds-1", site="site")["sources"][0]["transforms"]
    assert sans[0]["op"] == "naive_forecast"
    assert avec[0] == {"op": "aggregate", "group_by": ["semaine", "famille"], "aggregates": {"ventes": "SUM(ventes)"}}
    assert [t["op"] for t in avec[1:]] == [t["op"] for t in sans]


def test_pas_de_chevauchement_et_dans_la_grille(spec):
    taken: dict[tuple[int, int], int] = {}
    for idx, comp in enumerate(spec["components"]):
        p = comp["position"]
        assert 0 <= p["col"] and p["col"] + p["width"] <= 12, p
        for r in range(p["row"], p["row"] + p["height"]):
            for c in range(p["col"], p["col"] + p["width"]):
                assert (r, c) not in taken, f"composant {idx} chevauche {taken[(r, c)]}"
                taken[(r, c)] = idx


def test_chaque_mesure_referencee_existe(spec):
    sources = {s["name"]: s for s in spec["sources"]}
    measures = {m["name"] for m in sources["stocks"]["measures"]}
    fields = {f["name"] for f in sources["stocks"]["fields"]}
    assert not measures & fields, "un nom de mesure ne doit pas masquer un champ"
    tmpl = re.compile(r"\{\{\s*([a-z_][a-z0-9_]*)")
    for comp in spec["components"]:
        v = comp["visual"]
        if v["type"] == "note":
            continue
        assert v["source"] == "stocks"
        refs: set[str] = set()
        if v.get("kpi"):
            refs.add(v["kpi"]["measure"])
            refs |= set(tmpl.findall(v["kpi"].get("secondary", "")))
        refs |= {s["key"] for s in v.get("series", [])}
        refs |= {st["measure"] for st in v.get("steps", [])}
        refs |= {c["key"] for c in v.get("columns", [])} - {"famille"}
        if v["type"] == "text":
            refs |= {r for r in tmpl.findall(v["text"]) if r not in ("filters", "period")}
        assert refs <= measures | fields, (v.get("title"), refs - measures - fields)
    flt = {f["id"] for f in spec["filters"]}
    for comp in spec["components"]:
        v = comp["visual"]
        assert set(v.get("click_filters", {})) <= flt
        assert set(v.get("ignore_filters", [])) <= flt
        if v.get("highlight_filter"):
            assert v["highlight_filter"] in flt


def test_seuils_formats_et_provenances(spec):
    stocks = spec["sources"][0]
    m = {x["name"]: x for x in stocks["measures"]}
    assert m["couverture"]["thresholds"] == {"direction": "higher_is_better", "good": 1, "warn": 0.5}
    assert m["couverture"]["format"] == "weeks"
    assert m["taux_service"]["thresholds"]["good"] == 0.99 and m["taux_service"]["thresholds"]["warn"] == 0.97
    assert m["fiabilite"]["thresholds"]["good"] == 0.9 and m["fiabilite"]["thresholds"]["warn"] == 0.8
    assert m["semaines_sous_secu"]["thresholds"] == {"direction": "lower_is_better", "good": 0, "warn": 2}
    prov = {f["name"]: f.get("provenance", "real") for f in stocks["fields"]}
    assert prov["ventes"] == "real" and prov["prevision"] == "forecast"
    assert prov["stock_fin"] == prov["appro"] == "simulated"


def test_filtres_par_defaut_et_parametres():
    spec = stock_pilotage("ds-1", default_period="13w")
    famille, periode = spec["filters"]
    assert famille["type"] == "select" and "default" not in famille
    assert periode["type"] == "relative_period" and periode["default"] == "13w"
    assert periode["presets"] == ["13w", "26w", "52w", "all"]


def test_sans_prevision_moteur():
    spec = stock_pilotage("ds-1", with_engine_forecast=False)
    assert [s["name"] for s in spec["sources"]] == ["stocks"]
    assert spec["filters"][1].get("applies_to") is None


def test_notes_chiffrees_depuis_les_parametres():
    spec = stock_pilotage("ds-1", safety_factor=0.6, order_factor=1.5, lead_time=2, lot_sizes={"A": 50})
    notes = {c["visual"]["title"]: c["visual"]["text"] for c in spec["components"] if c["visual"]["type"] == "note"}
    assert len(notes) == 5
    regles = notes["Règles de réapprovisionnement simulées"]
    assert "0,6" in regles and "1,5" in regles and "2 semaines" in regles and "A : 50" in regles


@pytest.mark.parametrize("kwargs", [{"date": "Semaine"}, {"family": "a b"}, {"sales": "1x"}, {"site": "Site"}])
def test_noms_de_colonnes_invalides(kwargs):
    with pytest.raises(ValueError, match="not a valid column name"):
        stock_pilotage("ds-1", **kwargs)


@pytest.mark.skipif(not HAS_VALIDATE, reason="moteur de validation pas encore livré")
@pytest.mark.parametrize("site", [None, "site"])
def test_validate_spec(site):
    from apowerb.bi.spec.validate import validate_spec

    assert validate_spec(_dashboard(stock_pilotage("ds-1", site=site))) == []


# ---------------------------------------------------------------------------
# Intégration sans mock du moteur (données synthétiques)
# ---------------------------------------------------------------------------

HAS_ENGINE = importlib.util.find_spec("apowerb.bi.spec.engine") is not None


def _synthetic_sales():
    import math
    import random

    import pandas as pd

    rnd = random.Random(7)
    start = pd.Timestamp("2022-01-03")
    rows = []
    for fam_idx, fam in enumerate(["Famille A", "Famille B", "Famille C", "Famille D", "Famille E"]):
        base = 200 * (fam_idx + 1)
        for w in range(156):
            season = 1 + 0.3 * math.sin(2 * math.pi * w / 52)
            rows.append({"semaine": start + pd.Timedelta(weeks=w), "famille": fam,
                         "ventes": round(base * season * (0.9 + 0.2 * rnd.random()))})
    return pd.DataFrame(rows)


@pytest.mark.skipif(not HAS_ENGINE, reason="moteur pas encore livré")
@pytest.mark.parametrize("site", [None])
def test_integration_moteur_sans_mock(monkeypatch, site):
    import asyncio

    import apowerb.bi.spec.sources as sources
    from apowerb.bi.spec.engine import compute_dashboard
    from apowerb.bi.spec.models import DashboardDataRequest

    async def fake_load(dataset_id, owner, sheet=None):
        return _synthetic_sales()

    monkeypatch.setattr(sources, "load_dataset", fake_load)
    dash = _dashboard(stock_pilotage("ds-1", with_engine_forecast=False, site=site))
    resp = asyncio.run(compute_dashboard(dash, DashboardDataRequest(), owner="alice@example.com"))
    broken = {cid: r.error for cid, r in resp.components.items() if r.error}
    assert broken == {}
    kpis = {c.visual.title: resp.components[c.id].value for c in dash.components if c.visual.type == "kpi"}
    assert len(kpis) == 6 and all(v.value is not None for v in kpis.values())
    print({k: v.formatted for k, v in kpis.items()})


def test_combo_lisible_stock_en_aire_prevision_en_ligne(spec):
    """26 semaines × 3 séries de barres sont illisibles : une seule série en barres."""
    combo = next(c["visual"] for c in spec["components"] if c["visual"]["type"] == "combo")
    marks = {s["key"]: (s["mark"], s.get("dash", False)) for s in combo["series"]}
    assert marks == {
        "stock_total": ("area", False),
        "prevision_totale": ("line", False),
        "appro_total": ("bar", False),
        "securite_totale": ("line", True),
    }


@pytest.mark.skipif(not HAS_ENGINE, reason="moteur pas encore livré")
def test_kpi_sparkline_sur_la_periode_reelle(monkeypatch):
    """La mini-courbe suit la période filtrée (26 semaines) sans les semaines prévues."""
    import asyncio

    import apowerb.bi.spec.sources as sources
    from apowerb.bi.spec.engine import compute_dashboard
    from apowerb.bi.spec.models import DashboardDataRequest

    async def fake_load(dataset_id, owner, sheet=None):
        return _synthetic_sales()

    monkeypatch.setattr(sources, "load_dataset", fake_load)
    dash = _dashboard(stock_pilotage("ds-1", with_engine_forecast=False))
    resp = asyncio.run(compute_dashboard(dash, DashboardDataRequest(), owner="alice@example.com"))
    by_title = {c.visual.title: (c.visual, resp.components[c.id].value) for c in dash.components
                if c.visual.type == "kpi"}
    visual, stock = by_title["Stock fin de semaine"]
    assert visual.kpi.sparkline == "stock_total"
    assert stock.spark is not None and len(stock.spark) == 26
    assert stock.spark[-1] == pytest.approx(stock.value)
    sans = [v for v, val in by_title.values() if not v.kpi.sparkline]
    assert all(by_title[v.title][1].spark is None for v in sans)


@pytest.mark.skipif(not HAS_ENGINE, reason="moteur pas encore livré")
def test_ligne_toutes_du_tableau_egale_aux_kpi(monkeypatch):
    """La ligne « Toutes » reprend la définition globale, pas la somme des familles."""
    import asyncio

    import apowerb.bi.spec.sources as sources
    from apowerb.bi.spec.engine import compute_dashboard
    from apowerb.bi.spec.models import DashboardDataRequest

    async def fake_load(dataset_id, owner, sheet=None):
        df = _synthetic_sales()
        # Une famille en rupture sur une semaine, compensée par les autres : sous sécu
        # pour la famille, pas pour le total.
        df.loc[(df["famille"] == df["famille"].iloc[0]) & (df.index % 156 == 150), "ventes"] *= 6
        return df

    monkeypatch.setattr(sources, "load_dataset", fake_load)
    dash = _dashboard(stock_pilotage("ds-1", with_engine_forecast=False))
    resp = asyncio.run(compute_dashboard(dash, DashboardDataRequest(), owner="alice@example.com"))
    kpis = {c.visual.kpi.measure: resp.components[c.id].value.value for c in dash.components
            if c.visual.type == "kpi"}
    table = next(c for c in dash.components if c.visual.type == "table")
    rows = resp.components[table.id].rows
    total = next(r for r in rows if r.get("_total"))
    familles = [r for r in rows if not r.get("_total")]
    assert max(r["semaines_sous_secu_famille"] for r in familles) > kpis["semaines_sous_secu"]
    assert total["semaines_sous_secu_famille"] == kpis["semaines_sous_secu"]
    assert total["stock_fin_semaine"] == pytest.approx(kpis["stock_fin_semaine"])

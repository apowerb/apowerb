"""Gabarit « Pilotage stocks » avec une colonne de stock réel : plan
d'approvisionnement sur l'horizon (moteur complet, sans mock du calcul)."""
import asyncio
import math
import random

import pandas as pd
import pytest

from apowerb.bi.dashboards.core import ComponentType, Dashboard, DashboardComponent, GridPosition
from apowerb.bi.spec.models import DashboardDataRequest, FilterSpec, SourceSpec, VisualSpec
from apowerb.bi.spec.templates import stock_pilotage
from apowerb.bi.spec.validate import validate_spec

KW = dict(stock="stock", safety_column="seuil_securite", lead_time_column="delai_fournisseur_sem")


def _dashboard(spec: dict) -> Dashboard:
    return Dashboard.create(
        "Plan",
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


def _data():
    rnd = random.Random(3)
    start = pd.Timestamp("2023-01-02")
    rows = []
    for i, (fam, stock_end, seuil, delai) in enumerate(
        [("Cartons", 30_000, 6000, 2), ("Films", 3600, 3500, 3), ("Adhésifs", 2600, 1500, 4)]
    ):
        base = 1000 * (i + 2)
        for w in range(104):
            ventes = round(base * (1 + 0.2 * math.sin(2 * math.pi * w / 52)) * (0.9 + 0.2 * rnd.random()))
            rows.append({"semaine": start + pd.Timedelta(weeks=w), "famille": fam, "ventes": ventes,
                         "stock": stock_end if w == 103 else 5 * base, "seuil_securite": seuil,
                         "delai_fournisseur_sem": delai})
    return pd.DataFrame(rows)


def _fake_engine(body):
    last = max(pd.Timestamp(r["semaine"]) for r in body["data"])
    groups = sorted({r["famille"] for r in body["data"]})
    level = {"Cartons": 2000, "Films": 3000, "Adhésifs": 4000}
    return {"series": [
        {"group": g, "forecast": [{"date": (last + pd.Timedelta(weeks=k)).strftime("%Y-%m-%d"), "value": level[g]}
                                  for k in range(1, body["horizon"] + 1)]}
        for g in groups
    ]}


@pytest.fixture
def computed(monkeypatch):
    import apowerb.bi.spec.sources as sources
    import apowerb.bi.spec.transforms as transforms
    from apowerb.bi.spec.engine import compute_dashboard

    async def fake_load(dataset_id, owner, sheet=None):
        return _data()

    def run(engine=_fake_engine, **kw):
        transforms.clear_cache()
        monkeypatch.setattr(sources, "load_dataset", fake_load)
        monkeypatch.setattr(transforms, "_call_forecast_engine", engine)
        dash = _dashboard(stock_pilotage("ds-1", **KW, **kw))
        return dash, asyncio.run(compute_dashboard(dash, DashboardDataRequest(), owner="alice@example.com"))

    return run


def test_sans_colonne_stock_le_gabarit_est_inchange():
    assert stock_pilotage("ds-1") == stock_pilotage("ds-1", stock=None, target_cover=6, keep_history=3)


def test_spec_valide_et_sans_chevauchement():
    spec = stock_pilotage("ds-1", **KW)
    assert validate_spec(_dashboard(spec)) == []
    assert [s["name"] for s in spec["sources"]] == ["stocks", "plan", "horizon"]
    taken = set()
    for comp in spec["components"]:
        p = comp["position"]
        assert p["col"] + p["width"] <= 12
        cells = {(r, c) for r in range(p["row"], p["row"] + p["height"]) for c in range(p["col"], p["col"] + p["width"])}
        assert not cells & taken
        taken |= cells


def test_spec_valide_avec_site_et_sans_prevision_moteur():
    spec = stock_pilotage("ds-1", site="site", with_engine_forecast=False, **KW)
    assert validate_spec(_dashboard(spec)) == []
    ops = [t["op"] for t in spec["sources"][1]["transforms"]]
    assert ops == ["replenishment_plan"]


@pytest.mark.parametrize("kwargs", [{"stock": "Stock"}, {"stock": "stock", "safety_column": "a b"}])
def test_noms_de_colonnes_invalides(kwargs):
    with pytest.raises(ValueError, match="not a valid column name"):
        stock_pilotage("ds-1", **kwargs)


def test_tableau_de_bord_calcule_de_bout_en_bout(computed):
    dash, resp = computed()
    broken = {c.visual.title: resp.components[c.id].error for c in dash.components if resp.components[c.id].error}
    assert broken == {}
    kpis = {c.visual.kpi.measure: resp.components[c.id].value.value for c in dash.components if c.visual.type == "kpi"}
    assert kpis["stock_actuel"] == 30_000 + 3600 + 2600
    assert kpis["prevision_semaine"] == 2000 + 3000 + 4000
    # Films (3 600, seuil 3 500, délai 3) et Adhésifs (2 600, délai 4) passent
    # sous le seuil avant toute arrivée ; Cartons tient.
    assert kpis["familles_a_risque"] == 2
    assert kpis["commande_semaine"] > 0
    table = next(c for c in dash.components if c.visual.type == "table")
    rows = {r["famille"]: r for r in resp.components[table.id].rows if not r.get("_total")}
    assert set(rows) == {"Cartons", "Films", "Adhésifs"}
    assert rows["Cartons"]["semaines_avant_rupture"] == 8
    assert rows["Films"]["semaines_avant_rupture"] == 1
    assert rows["Adhésifs"]["semaines_avant_rupture"] == 0
    # ligne « Toutes » : la famille la plus exposée, pas la somme
    total = next(r for r in resp.components[table.id].rows if r.get("_total"))
    assert total["semaines_avant_rupture"] == 0
    assert not resp.sources["plan"].warnings
    # Commandes à passer : seulement les semaines de l'horizon, sans l'historique.
    bar = next(c for c in dash.components if c.visual.type == "bar")
    assert bar.visual.title.startswith("Commandes")
    pts = resp.components[bar.id].rows
    assert len(pts) == 8
    assert min(pd.Timestamp(r["semaine"]) for r in pts) > pd.Timestamp("2023-01-02") + pd.Timedelta(weeks=103)


def test_moteur_indisponible_plan_quand_meme_avec_avertissement(computed):
    from apowerb.bi.spec.transforms import ForecastUnavailable

    def down(body):
        raise ForecastUnavailable("service de prévision indisponible")

    dash, resp = computed(engine=down)
    broken = {c.visual.title: resp.components[c.id].error for c in dash.components if resp.components[c.id].error}
    assert broken == {}
    kpis = {c.visual.kpi.measure: resp.components[c.id].value.value for c in dash.components if c.visual.type == "kpi"}
    assert kpis["prevision_semaine"] is not None and kpis["prevision_semaine"] > 0
    assert any("moyenne des 4 dernières semaines" in w for w in resp.sources["plan"].warnings)
    prevision = next(c for c in dash.components if c.visual.type == "kpi" and c.visual.kpi.measure == "prevision_semaine")
    assert resp.components[prevision.id].meta.warnings == [
        "Prévision indisponible : plan calculé avec la moyenne des 4 dernières semaines de ventes."
    ]

"""Gabarit « Qualité production » : spec valide, grille sans chevauchement, mesures
existantes, moteur réel sur un jeu synthétique (colonnes par défaut, renommées, sans optionnelles)."""
import asyncio
import random
import re

import pandas as pd
import pytest

from apowerb.bi.dashboards.core import ComponentType, Dashboard, DashboardComponent, GridPosition
from apowerb.bi.spec.formatting import format_value
from apowerb.bi.spec.models import DashboardDataRequest, FilterSpec, SourceSpec, VisualSpec
from apowerb.bi.spec.templates_quality import DESCRIPTOR, build, production_quality
from apowerb.bi.spec.validate import validate_spec

BASE_KPIS = ["Taux de rebut", "Bon du premier coup", "Volume produit", "Volume rebuté"]
TRS = dict(planned_time="temps_planifie", run_time="temps_marche", ideal_rate="cadence")
FULL = dict(product="produit", defect="cause", **TRS)


def _dashboard(spec: dict) -> Dashboard:
    return Dashboard.create(
        "Qualité",
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


def _synthetic_production() -> pd.DataFrame:
    rnd = random.Random(5)
    start = pd.Timestamp("2025-04-28")
    lines = ["Ligne 1", "Ligne 2", "Ligne 3", "Ligne 4"]
    products = [f"Produit {c}" for c in "ABCDEF"]
    causes = ["Rayure", "Dimension", "Soudure", "Peinture", "Assemblage"]
    rows = []
    for week in range(26):
        for li, line in enumerate(lines):
            for product in products:
                planned = [40.0, 40.0, 60.0, 20.0][li]
                run = planned * rnd.uniform(0.78, 0.95)
                rate = 100.0
                produced = round(run * rate * rnd.uniform(0.75, 1.0))
                reject_rate = rnd.uniform(0.005, 0.03) * (2.2 if line == "Ligne 4" else 1.0)
                rejected = round(produced * reject_rate)
                rows.append({
                    "date": start + pd.Timedelta(weeks=week, days=rnd.randrange(0, 5)),
                    "ligne": line,
                    "produit": product,
                    "qte_produite": produced,
                    "qte_rebut": rejected,
                    "cause": rnd.choices(causes, weights=[5, 3, 2, 1, 1])[0] if rejected else None,
                    "temps_planifie": planned,
                    "temps_marche": run,
                    "cadence": rate,
                })
    return pd.DataFrame(rows)


def _compute(monkeypatch, spec: dict, df: pd.DataFrame, filters: dict | None = None):
    import apowerb.bi.spec.sources as sources
    from apowerb.bi.spec.engine import compute_dashboard

    async def fake_load(dataset_id, owner, sheet=None):
        return df

    monkeypatch.setattr(sources, "load_dataset", fake_load)
    dash = _dashboard(spec)
    resp = asyncio.run(compute_dashboard(dash, DashboardDataRequest(filters=filters or {}), owner="alice@example.com"))
    return dash, resp


def _kpis(dash, resp) -> dict:
    return {c.visual.title: resp.components[c.id].value for c in dash.components if c.visual.type == "kpi"}


def _component(dash, resp, title):
    comp = next(c for c in dash.components if c.visual.title == title)
    return comp.visual, resp.components[comp.id]


def _expected(df: pd.DataFrame, weeks: int = 12, line: str | None = None) -> dict:
    last = df["date"].max()
    window = df[df["date"] > last - pd.Timedelta(weeks=weeks)]
    if line:
        window = window[window["ligne"] == line]
    produced, rejected = window["qte_produite"].sum(), window["qte_rebut"].sum()
    availability = window["temps_marche"].sum() / window["temps_planifie"].sum()
    performance = min(1.0, produced / (window["temps_marche"] * window["cadence"]).sum())
    quality = (produced - rejected) / produced
    return {
        "taux_rebut": rejected / produced,
        "bon": 1 - rejected / produced,
        "produit": float(produced),
        "rebut": float(rejected),
        "dispo": availability,
        "perf": performance,
        "trs": availability * performance * quality,
        "causes": window.groupby("cause")["qte_rebut"].sum().sort_values(ascending=False),
    }


@pytest.fixture(params=[{}, FULL], ids=["sans_optionnelles", "complet"])
def spec(request):
    return production_quality("ds-1", **request.param)


def test_spec_valide(spec):
    assert validate_spec(_dashboard(spec)) == []


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
    (source,) = spec["sources"]
    measures = {m["name"] for m in source["measures"]}
    fields = {f["name"] for f in source["fields"]}
    assert not measures & fields
    tmpl = re.compile(r"\{\{\s*([a-z_][a-z0-9_]*)")
    for comp in spec["components"]:
        v = comp["visual"]
        if v["type"] == "note":
            continue
        assert v["source"] == source["name"]
        refs: set[str] = set()
        if v.get("kpi"):
            refs |= {v["kpi"]["measure"], *tmpl.findall(v["kpi"].get("secondary", ""))}
            refs |= {v["kpi"]["sparkline"]} if v["kpi"].get("sparkline") else set()
        refs |= {s["key"] for s in v.get("series", [])}
        refs |= {c["key"] for c in v.get("columns", [])} - {"ligne"}
        if v["type"] == "text":
            refs |= {r for r in tmpl.findall(v["text"]) if r not in ("filters", "period")}
        assert refs <= measures | fields, (v.get("title"), refs - measures - fields)


def test_seuils_et_lignes_de_reference():
    spec = production_quality("ds-1", **TRS)
    m = {x["name"]: x for x in spec["sources"][0]["measures"]}
    assert m["taux_rebut"]["thresholds"] == {"direction": "lower_is_better", "good": 0.02, "warn": 0.05}
    assert m["trs"]["thresholds"] == {"direction": "higher_is_better", "good": 0.85, "warn": 0.6}
    combo = next(c["visual"] for c in spec["components"] if c["visual"]["type"] == "combo")
    assert [r["value"] for r in combo["reference_lines"]] == [0.02]


def test_composants_conditionnels():
    def titles(s):
        return {c["visual"].get("title") for c in s["components"]}

    base = production_quality("ds-1")
    assert titles(base).isdisjoint({"TRS", "Taux de rebut par produit", "Causes de rebut"})
    assert {m["name"] for m in base["sources"][0]["measures"]}.isdisjoint({"trs", "disponibilite", "performance"})
    assert {f["id"] for f in base["filters"]} == {"periode", "ligne"}
    full = production_quality("ds-1", **FULL)
    assert {"TRS", "Taux de rebut par produit", "Causes de rebut"} <= titles(full)
    assert {f["id"] for f in full["filters"]} == {"periode", "ligne", "produit"}
    # TRS : les trois colonnes sont nécessaires
    partial = production_quality("ds-1", planned_time="temps_planifie", run_time="temps_marche")
    assert "TRS" not in titles(partial)


@pytest.mark.parametrize(
    "kwargs",
    [{"date": "Date"}, {"line": "a b"}, {"produced": "1x"}, {"product": "Produit"}, {"defect": "c-1"}, {"ideal_rate": "Cad"}],
)
def test_noms_de_colonnes_invalides(kwargs):
    with pytest.raises(ValueError, match="not a valid column name"):
        production_quality("ds-1", **kwargs)


def test_descripteur():
    assert DESCRIPTOR["id"] == "production_quality" and DESCRIPTOR["version"] == 1 and DESCRIPTOR["params"] == []
    assert {"id", "label", "description", "version", "columns", "params"} == set(DESCRIPTOR)
    cols = {c["key"]: c for c in DESCRIPTOR["columns"]}
    assert set(cols) == {
        "date", "line", "product", "produced", "rejected", "defect", "planned_time", "run_time", "ideal_rate",
    }
    assert all(set(c) == {"key", "label", "type", "required", "default"} for c in cols.values())
    assert {k for k, c in cols.items() if c["required"]} == {"date", "line", "produced", "rejected"}
    assert cols["date"]["type"] == "date" and cols["produced"]["type"] == "number"
    assert cols["line"]["default"] == "ligne" and cols["defect"]["default"] is None


def test_build_appelle_le_gabarit_par_mots_cles():
    columns = {c["key"]: c["default"] for c in DESCRIPTOR["columns"]}
    assert build("ds-1", columns, {}) == production_quality("ds-1")
    columns.update(product="produit", planned_time="tp", run_time="tm", ideal_rate="cad", line="poste")
    expected = production_quality("ds-1", line="poste", product="produit", planned_time="tp", run_time="tm", ideal_rate="cad")
    assert build("ds-1", columns, {}) == expected
    columns["product"] = ""
    assert build("ds-1", columns, {}) == production_quality(
        "ds-1", line="poste", planned_time="tp", run_time="tm", ideal_rate="cad"
    )


def test_build_colonne_requise_vide():
    columns = {c["key"]: c["default"] for c in DESCRIPTOR["columns"]}
    columns["line"] = None
    with pytest.raises(ValueError, match="not a valid column name"):
        build("ds-1", columns, {})


# ---------------------------------------------------------------------------
# Intégration sans mock du moteur
# ---------------------------------------------------------------------------


def test_integration_moteur_sans_mock(monkeypatch):
    df = _synthetic_production()
    dash, resp = _compute(monkeypatch, production_quality("ds-1", **FULL), df)
    assert {cid: r.error for cid, r in resp.components.items() if r.error} == {}
    kpis = _kpis(dash, resp)
    assert list(kpis) == [*BASE_KPIS, "TRS"]
    assert all(v.value is not None for v in kpis.values())
    exp = _expected(df)
    assert kpis["Taux de rebut"].value == pytest.approx(exp["taux_rebut"])
    assert 0.01 < kpis["Taux de rebut"].value < 0.05
    assert kpis["Bon du premier coup"].value == pytest.approx(exp["bon"])
    assert kpis["Volume produit"].value == pytest.approx(exp["produit"])
    assert kpis["Volume rebuté"].value == pytest.approx(exp["rebut"])
    assert kpis["TRS"].value == pytest.approx(exp["trs"])
    assert 0.5 < kpis["TRS"].value < 0.85
    assert kpis["Taux de rebut"].status in ("good", "warn")
    assert kpis["TRS"].status in ("warn", "bad")
    for kpi in kpis.values():
        assert kpi.spark is not None and 12 <= len(kpi.spark) <= 13
    print({k: v.formatted for k, v in kpis.items()})


def test_ligne_toutes_calculee_sur_le_total(monkeypatch):
    df = _synthetic_production()
    dash, resp = _compute(monkeypatch, production_quality("ds-1", **FULL), df)
    kpis = _kpis(dash, resp)
    _, table = _component(dash, resp, "Performance par ligne")
    total = next(r for r in table.rows if r.get("_total"))
    lines = [r for r in table.rows if not r.get("_total")]
    assert len(lines) == 4 and total["ligne"] == "Toutes"
    assert total["taux_rebut"] == pytest.approx(kpis["Taux de rebut"].value)
    assert total["trs"] == pytest.approx(kpis["TRS"].value)
    assert total["volume_produit"] == pytest.approx(sum(r["volume_produit"] for r in lines))
    assert total["volume_rebut"] == pytest.approx(sum(r["volume_rebut"] for r in lines))
    assert total["trs"] != pytest.approx(sum(r["trs"] for r in lines) / len(lines), abs=1e-9)
    worst = max(lines, key=lambda r: r["taux_rebut"])
    assert worst["ligne"] == "Ligne 4"


def test_graphiques_et_pareto(monkeypatch):
    df = _synthetic_production()
    dash, resp = _compute(monkeypatch, production_quality("ds-1", **FULL), df)
    exp = _expected(df)
    _, par_ligne = _component(dash, resp, "Taux de rebut par ligne")
    rates = [r["taux_rebut"] for r in par_ligne.rows]
    assert len(rates) == 4 and rates == sorted(rates, reverse=True)
    _, par_produit = _component(dash, resp, "Taux de rebut par produit")
    assert len(par_produit.rows) == 6
    _, pareto = _component(dash, resp, "Causes de rebut")
    assert [r["x"] for r in pareto.rows] == list(exp["causes"].index)
    assert [r["volume_rebut"] for r in pareto.rows] == pytest.approx(list(exp["causes"].values))
    _, cumul = _component(dash, resp, "Part cumulée des causes")
    cum = exp["causes"].cumsum() / exp["rebut"]
    assert format_value(cum.iloc[0], "percent") in cumul.text
    assert format_value(cum.iloc[2], "percent") in cumul.text
    _, evolution = _component(dash, resp, "Taux de rebut par semaine")
    assert 12 <= len(evolution.rows) <= 13
    assert evolution.reference_lines[0]["value"] == 0.02


def test_filtre_ligne(monkeypatch):
    df = _synthetic_production()
    dash, resp = _compute(monkeypatch, production_quality("ds-1", **FULL), df, {"ligne": "Ligne 4"})
    assert {cid: r.error for cid, r in resp.components.items() if r.error} == {}
    kpis = _kpis(dash, resp)
    exp = _expected(df, line="Ligne 4")
    assert kpis["Taux de rebut"].value == pytest.approx(exp["taux_rebut"])
    assert kpis["TRS"].value == pytest.approx(exp["trs"])


def test_colonnes_renommees(monkeypatch):
    df = _synthetic_production().rename(columns={
        "date": "jour", "ligne": "poste", "produit": "article", "qte_produite": "bons_et_rebuts", "qte_rebut": "rebuts",
        "cause": "defaut", "temps_planifie": "h_planifiees", "temps_marche": "h_marche", "cadence": "pieces_par_h",
    })
    spec = production_quality(
        "ds-1", date="jour", line="poste", product="article", produced="bons_et_rebuts", rejected="rebuts",
        defect="defaut", planned_time="h_planifiees", run_time="h_marche", ideal_rate="pieces_par_h",
    )
    assert validate_spec(_dashboard(spec)) == []
    dash, resp = _compute(monkeypatch, spec, df)
    assert {cid: r.error for cid, r in resp.components.items() if r.error} == {}
    kpis = _kpis(dash, resp)
    assert all(v.value is not None for v in kpis.values())
    assert kpis["TRS"].value == pytest.approx(_expected(_synthetic_production())["trs"])


def test_colonne_date_nommee_semaine(monkeypatch):
    df = _synthetic_production().rename(columns={"date": "semaine"})
    spec = production_quality("ds-1", date="semaine", **FULL)
    dash, resp = _compute(monkeypatch, spec, df)
    assert {cid: r.error for cid, r in resp.components.items() if r.error} == {}
    assert _kpis(dash, resp)["Taux de rebut"].value == pytest.approx(_expected(_synthetic_production())["taux_rebut"])


def test_sans_colonnes_optionnelles(monkeypatch):
    df = _synthetic_production().drop(columns=["produit", "cause", "temps_planifie", "temps_marche", "cadence"])
    dash, resp = _compute(monkeypatch, production_quality("ds-1"), df)
    assert {cid: r.error for cid, r in resp.components.items() if r.error} == {}
    kpis = _kpis(dash, resp)
    assert list(kpis) == BASE_KPIS and all(v.value is not None for v in kpis.values())
    titles = {c.visual.title for c in dash.components}
    assert titles.isdisjoint({"TRS", "Taux de rebut par produit", "Causes de rebut"})
    _, table = _component(dash, resp, "Performance par ligne")
    assert "trs" not in table.rows[0]


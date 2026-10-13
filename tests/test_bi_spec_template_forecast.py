"""Gabarit « Prévision des ventes » : spec valide, grille sans chevauchement, mesures référencées
existantes, moteur réel sans mock du calcul (prévision naïve, moteur simulé à la frontière
réseau, repli quand le moteur est indisponible)."""
import asyncio
import re

import numpy as np
import pandas as pd
import pytest

from apowerb.bi.dashboards.core import ComponentType, Dashboard, DashboardComponent, GridPosition
from apowerb.bi.spec.models import DashboardDataRequest, FilterSpec, SourceSpec, VisualSpec
from apowerb.bi.spec.templates_forecast import DESCRIPTOR, build, sales_forecast
from apowerb.bi.spec.transforms import ForecastUnavailable, naive_forecast_series
from apowerb.bi.spec.validate import validate_spec

FAMILIES = ["Cartons", "Films", "Adhésifs"]
RENAMED = dict(date="jour", family="gamme", sales="qte")
HORIZON = 6
TITLES = {
    "total": "Total prévu sur l'horizon",
    "evolution": "Écart vs même période N-1",
    "fiabilite": "Fiabilité (test sur le passé)",
    "pic": "Semaine de pic",
}


def _dashboard(spec: dict) -> Dashboard:
    return Dashboard.create(
        "Prévision des ventes",
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


def _names(kwargs: dict) -> dict:
    return {"date": "semaine", "family": "famille", "sales": "ventes", **kwargs}


def _synthetic(kwargs: dict, weeks: int = 130) -> pd.DataFrame:
    n = _names(kwargs)
    rnd = np.random.default_rng(5)
    start = pd.Timestamp("2022-01-03")
    rows = []
    for i, fam in enumerate(FAMILIES):
        base = 300 * (i + 1)
        for w in range(weeks):
            season = 1 + 0.3 * np.sin(2 * np.pi * w / 52)
            rows.append({n["date"]: start + pd.Timedelta(weeks=w), n["family"]: fam,
                         n["sales"]: round(base * season * (0.9 + 0.2 * rnd.random()))})
    return pd.DataFrame(rows)


@pytest.fixture(params=[{}, RENAMED], ids=["defaut", "colonnes_renommees"])
def kwargs(request):
    return request.param


@pytest.fixture(params=[True, False], ids=["avec_moteur", "sans_moteur"])
def spec(request, kwargs):
    return sales_forecast("ds-1", horizon=HORIZON, with_engine_forecast=request.param, **kwargs)


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
    sources = {s["name"]: s for s in spec["sources"]}
    assert list(sources) == ["ventes"]
    measures = {m["name"] for m in sources["ventes"]["measures"]}
    fields = {f["name"] for f in sources["ventes"]["fields"]}
    assert not measures & fields, "un nom de mesure ne doit pas masquer un champ"
    tmpl = re.compile(r"\{\{\s*([a-z_][a-z0-9_]*)")
    for comp in spec["components"]:
        v = comp["visual"]
        if v["type"] == "note":
            continue
        assert v["source"] == "ventes"
        refs: set[str] = set()
        if v.get("kpi"):
            refs |= {v["kpi"]["measure"], v["kpi"].get("sparkline") or v["kpi"]["measure"]}
            refs |= set(tmpl.findall(v["kpi"].get("secondary", "")))
        refs |= {s["key"] for s in v.get("series", [])}
        refs |= {c["key"] for c in v.get("columns", [])} - {v.get("x")}
        if v["type"] == "text":
            refs |= {r for r in tmpl.findall(v["text"]) if r not in ("filters", "period")}
        assert refs <= measures | fields, (v.get("title"), refs - measures - fields)
    flt = {f["id"] for f in spec["filters"]}
    for comp in spec["components"]:
        v = comp["visual"]
        assert set(v.get("click_filters", {})) <= flt
        assert set(v.get("ignore_filters", [])) <= flt


def test_le_moteur_n_est_appele_que_si_demande():
    def ops(**kw):
        return [t["op"] for t in sales_forecast("ds-1", **kw)["sources"][0]["transforms"]]

    assert "forecast" in ops() and "forecast" not in ops(with_engine_forecast=False)
    engine = next(t for t in sales_forecast("ds-1", horizon=12)["sources"][0]["transforms"] if t["op"] == "forecast")
    assert engine["horizon"] == 12 and engine["by"] == ["famille"]


@pytest.mark.parametrize("bad", [{"date": "Semaine"}, {"family": "a b"}, {"sales": "1x"}])
def test_noms_de_colonnes_invalides(bad):
    with pytest.raises(ValueError, match="not a valid column name"):
        sales_forecast("ds-1", **bad)


@pytest.mark.parametrize("horizon", [0, 53])
def test_horizon_hors_limites(horizon):
    with pytest.raises(ValueError, match="horizon"):
        sales_forecast("ds-1", horizon=horizon)


def test_descripteur_et_build():
    assert DESCRIPTOR["id"] == "sales_forecast" and DESCRIPTOR["version"] == 1
    cols = {c["key"]: c for c in DESCRIPTOR["columns"]}
    assert list(cols) == ["date", "family", "sales"] and all(c["required"] for c in cols.values())
    assert [c["default"] for c in cols.values()] == ["semaine", "famille", "ventes"]
    assert all(set(c) == {"key", "label", "type", "required", "default"} for c in cols.values())
    assert [p["key"] for p in DESCRIPTOR["params"]] == ["horizon"]
    assert all(set(p) == {"key", "label", "type", "default", "min", "max"} for p in DESCRIPTOR["params"])
    assert build("ds-1", {"date": "semaine", "family": "famille", "sales": "ventes"}, {"horizon": 8}) == sales_forecast("ds-1")
    assert build("ds-1", {"date": "jour", "family": "gamme", "sales": "qte"}, {"horizon": 4}) == sales_forecast(
        "ds-1", horizon=4, **RENAMED)


# ---------------------------------------------------------------------------
# Moteur réel
# ---------------------------------------------------------------------------


def _fake_engine(levels: dict[str, float] | None = None, unavailable: bool = False):
    """Frontière réseau th2forecast : une valeur constante par famille et par semaine à venir."""

    def engine(body):
        if unavailable:
            raise ForecastUnavailable("service de prévision indisponible : test")
        date_var, group_var = body["date_var"], body["group_var"]
        out = []
        for fam in sorted({r[group_var] for r in body["data"]}):
            if levels is not None and fam not in levels:
                continue
            last = max(pd.Timestamp(r[date_var]) for r in body["data"] if r[group_var] == fam)
            out.append({"group": fam, "model": "prophet", "forecast": [
                {"date": (last + pd.Timedelta(weeks=k)).strftime("%Y-%m-%d"), "value": (levels or {}).get(fam, 0.0)}
                for k in range(1, body["horizon"] + 1)]})
        return {"status": "success", "series": out}

    return engine


def _compute(monkeypatch, kwargs, *, engine=None, with_engine_forecast=True, filters=None, weeks=130):
    import apowerb.bi.spec.sources as sources
    import apowerb.bi.spec.transforms as transforms
    from apowerb.bi.spec.engine import clear_caches, compute_dashboard

    frame = _synthetic(kwargs, weeks)

    async def fake_load(dataset_id, owner, sheet=None):
        return frame

    clear_caches()
    monkeypatch.setattr(sources, "load_dataset", fake_load)
    if engine is not None:
        monkeypatch.setattr(transforms, "_call_forecast_engine", engine)
    dash = _dashboard(sales_forecast("ds-1", horizon=HORIZON, with_engine_forecast=with_engine_forecast, **kwargs))
    req = DashboardDataRequest(filters=filters or {})
    return dash, asyncio.run(compute_dashboard(dash, req, owner="alice@example.com")), frame


def _kpis(dash, resp):
    return {c.visual.title: resp.components[c.id].value for c in dash.components if c.visual.type == "kpi"}


def _table(dash, resp):
    comp = next(c for c in dash.components if c.visual.type == "table")
    rows = resp.components[comp.id].rows
    cols = {c.label: c.key for c in comp.visual.columns}
    families = {r[comp.visual.x]: r for r in rows if not r.get("_total")}
    return families, next(r for r in rows if r.get("_total")), cols


def _naive_future(frame, kwargs):
    """Prévision naïve des HORIZON semaines suivantes, recalculée avec la fonction pure, par famille."""
    n = _names(kwargs)
    out = {}
    for fam, grp in frame.groupby(n["family"]):
        values = grp.sort_values(n["date"])[n["sales"]].to_numpy(dtype=float)
        padded = np.concatenate([values, np.full(HORIZON, np.nan)])
        out[fam] = naive_forecast_series(padded, window=4, season_length=52, trend_bounds=(0.8, 1.25))[len(values):]
    return out


def _assert_no_errors(resp):
    assert {cid: r.error for cid, r in resp.components.items() if r.error} == {}


def test_integration_moteur_sans_mock_prevision_naive(monkeypatch, kwargs):
    dash, resp, frame = _compute(monkeypatch, kwargs, with_engine_forecast=False)
    _assert_no_errors(resp)
    kpis = _kpis(dash, resp)
    assert len(kpis) == 4 and all(v.value is not None for v in kpis.values()), {k: v.formatted for k, v in kpis.items()}
    print({k: v.formatted for k, v in kpis.items()})
    families, total, cols = _table(dash, resp)
    want = {fam: float(vals.sum()) for fam, vals in _naive_future(frame, kwargs).items()}
    assert {f: r[cols["Prévu"]] for f, r in families.items()} == pytest.approx(want)
    assert total[cols["Prévu"]] == pytest.approx(sum(want.values()))
    assert kpis[TITLES["total"]].value == pytest.approx(sum(want.values()))
    assert {r[cols["Méthode"]] for r in families.values()} == {"Prévision naïve"}


def test_la_derniere_date_reelle_ne_bouge_pas_avec_les_semaines_prevues(monkeypatch):
    _, resp, frame = _compute(monkeypatch, {}, with_engine_forecast=False)
    assert resp.sources["ventes"].last_date == frame["semaine"].max().strftime("%Y-%m-%d")
    assert resp.sources["ventes"].row_count == len(frame) + HORIZON * len(FAMILIES)


def test_evolution_vs_meme_periode_n_moins_1(monkeypatch, kwargs):
    n = _names(kwargs)
    dash, resp, frame = _compute(monkeypatch, kwargs, with_engine_forecast=False)
    families, total, cols = _table(dash, resp)
    future = _naive_future(frame, kwargs)
    last = frame[n["date"]].max()
    days = [last + pd.Timedelta(weeks=k) for k in range(1, HORIZON + 1)]
    last_year = frame[frame[n["date"]].isin([d - pd.Timedelta(days=364) for d in days])]
    for fam, row in families.items():
        n1 = last_year[last_year[n["family"]] == fam][n["sales"]].sum()
        assert row[cols["N-1"]] == pytest.approx(n1)
        assert row[cols["Évolution vs N-1"]] == pytest.approx(future[fam].sum() / n1 - 1)
    n1_total = last_year[n["sales"]].sum()
    assert total[cols["Évolution vs N-1"]] == pytest.approx(sum(v.sum() for v in future.values()) / n1_total - 1)
    assert _kpis(dash, resp)[TITLES["evolution"]].value == pytest.approx(total[cols["Évolution vs N-1"]])


def test_fiabilite_est_le_test_de_la_methode_naive_sur_la_periode(monkeypatch):
    n = _names({})
    dash, resp, frame = _compute(monkeypatch, {}, with_engine_forecast=False, filters={"periode": "52w"})
    err = den = 0.0
    for _, grp in frame.groupby(n["family"]):
        grp = grp.sort_values(n["date"])
        naive = naive_forecast_series(grp[n["sales"]].to_numpy(dtype=float), window=4, season_length=52,
                                      trend_bounds=(0.8, 1.25))
        keep = (grp[n["date"]] > frame[n["date"]].max() - pd.Timedelta(weeks=52)).to_numpy() & ~np.isnan(naive)
        err += np.abs(grp[n["sales"]].to_numpy()[keep] - naive[keep]).sum()
        den += grp[n["sales"]].to_numpy()[keep].sum()
    assert _kpis(dash, resp)[TITLES["fiabilite"]].value == pytest.approx(1 - err / den)


def test_semaine_de_pic(monkeypatch):
    dash, resp, frame = _compute(monkeypatch, {}, with_engine_forecast=False)
    future = _naive_future(frame, {})
    weekly = sum(future.values())
    peak = int(np.argmax(weekly)) + 1
    day = frame["semaine"].max() + pd.Timedelta(weeks=peak)
    assert _kpis(dash, resp)[TITLES["pic"]].value == day.isocalendar().week
    comp = next(c for c in dash.components if c.visual.title == TITLES["pic"])
    assert resp.components[comp.id].value.secondary is not None


def test_avec_le_moteur_les_valeurs_du_moteur_priment(monkeypatch):
    levels = {"Cartons": 100.0, "Films": 200.0, "Adhésifs": 300.0}
    dash, resp, _ = _compute(monkeypatch, {}, engine=_fake_engine(levels))
    _assert_no_errors(resp)
    families, total, cols = _table(dash, resp)
    assert {f: r[cols["Prévu"]] for f, r in families.items()} == {f: v * HORIZON for f, v in levels.items()}
    assert total[cols["Prévu"]] == sum(levels.values()) * HORIZON
    assert {r[cols["Méthode"]] for r in families.values()} == {"Moteur th2forecast"}
    assert total[cols["Méthode"]] == "Moteur th2forecast"
    assert _kpis(dash, resp)[TITLES["total"]].value == sum(levels.values()) * HORIZON


def test_moteur_indisponible_repli_sur_la_prevision_naive(monkeypatch, kwargs):
    dash, resp, frame = _compute(monkeypatch, kwargs, engine=_fake_engine(unavailable=True))
    _assert_no_errors(resp)
    families, total, cols = _table(dash, resp)
    want = {fam: float(vals.sum()) for fam, vals in _naive_future(frame, kwargs).items()}
    assert {f: r[cols["Prévu"]] for f, r in families.items()} == pytest.approx(want)
    assert {r[cols["Méthode"]] for r in families.values()} == {"Prévision naïve (repli)"}
    assert "indisponible" in " ".join(resp.sources["ventes"].warnings)


def test_moteur_partiel_chaque_famille_garde_sa_methode(monkeypatch):
    dash, resp, frame = _compute(monkeypatch, {}, engine=_fake_engine({"Films": 500.0}))
    families, total, cols = _table(dash, resp)
    naive = _naive_future(frame, {})
    assert families["Films"][cols["Prévu"]] == 500.0 * HORIZON
    assert families["Films"][cols["Méthode"]] == "Moteur th2forecast"
    for fam in ("Cartons", "Adhésifs"):
        assert families[fam][cols["Prévu"]] == pytest.approx(naive[fam].sum())
        assert families[fam][cols["Méthode"]] == "Prévision naïve (repli)"
    assert total[cols["Méthode"]] == "Mixte"


def test_combo_historique_prevision_et_n_moins_1(monkeypatch):
    dash, resp, frame = _compute(monkeypatch, {}, with_engine_forecast=False, filters={"periode": "26w"})
    combo = next(c for c in dash.components if c.visual.type == "combo")
    rows = resp.components[combo.id].rows
    ventes, prevision, n1 = [s.key for s in combo.visual.series]
    assert len(rows) == 26 + HORIZON
    history, future = rows[:26], rows[26:]
    assert all(r[ventes] is not None for r in history) and all(r[ventes] is None for r in future)
    assert all(r[prevision] is not None for r in rows)
    assert all(r[n1] is not None for r in rows)


def test_famille_filtree(monkeypatch):
    dash, resp, frame = _compute(monkeypatch, {}, with_engine_forecast=False, filters={"famille": "Films"})
    want = _naive_future(frame, {})["Films"].sum()
    assert _kpis(dash, resp)[TITLES["total"]].value == pytest.approx(want)

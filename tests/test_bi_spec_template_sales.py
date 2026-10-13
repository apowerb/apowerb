"""Gabarit « Suivi des ventes » : spec valide, grille sans chevauchement, mesures référencées
existantes, moteur réel sans mock sur un jeu synthétique (valeurs recalculées avec pandas)."""
import asyncio
import re

import numpy as np
import pandas as pd
import pytest

from apowerb.bi.dashboards.core import ComponentType, Dashboard, DashboardComponent, GridPosition
from apowerb.bi.spec.models import DashboardDataRequest, FilterSpec, SourceSpec, VisualSpec
from apowerb.bi.spec.templates_sales import DESCRIPTOR, build, sales_tracking
from apowerb.bi.spec.validate import validate_spec

FULL = dict(quantity="quantite", customer="client", region="region")
RENAMED = dict(date="jour", amount="ca_ht", quantity="qte", family="gamme", customer="client_id", region="zone")
VARIANTS = {
    "minimal": {},
    "complet": FULL,
    "colonnes_renommees": RENAMED,
}
TITLES = {
    "ca": "Chiffre d'affaires",
    "evolution": "Évolution vs même période N-1",
}


def _dashboard(spec: dict) -> Dashboard:
    return Dashboard.create(
        "Suivi des ventes",
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
    return {"date": "date", "amount": "montant", "quantity": None, "family": "famille",
            "customer": None, "region": None, **kwargs}


def _synthetic(kwargs: dict) -> pd.DataFrame:
    """Ventes quotidiennes de février 2022 à fin juin 2024 (dernier mois complet)."""
    n = _names(kwargs)
    rnd = np.random.default_rng(11)
    rows = []
    families = ["Cartons", "Films", "Adhésifs", "Étiquettes"]
    for day in pd.date_range("2022-02-01", "2024-06-30", freq="D"):
        season = 1 + 0.3 * np.sin(2 * np.pi * day.dayofyear / 365)
        growth = 1 + 0.1 * (day.year - 2022)
        for i, fam in enumerate(families):
            if rnd.random() < 0.6:
                qty = int(rnd.integers(5, 40))
                rows.append({
                    n["date"]: day, n["family"]: fam,
                    n["amount"]: round(qty * (8 + 3 * i) * season * growth, 2),
                    "_qty": qty,
                    "_cust": f"Client {int(rnd.integers(1, 15)):02d}",
                    "_reg": ["Nord", "Sud", "Est"][int(rnd.integers(0, 3))],
                })
    df = pd.DataFrame(rows)
    out = df.drop(columns=["_qty", "_cust", "_reg"])
    if n["quantity"]:
        out[n["quantity"]] = df["_qty"]
    if n["customer"]:
        out[n["customer"]] = df["_cust"]
    if n["region"]:
        out[n["region"]] = df["_reg"]
    return out


@pytest.fixture(params=list(VARIANTS), ids=list(VARIANTS))
def kwargs(request):
    return VARIANTS[request.param]


@pytest.fixture
def spec(kwargs):
    return sales_tracking("ds-1", **kwargs)


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
        refs |= {c["total_measure"] for c in v.get("columns", []) if c.get("total_measure")}
        if v["type"] == "text":
            refs |= {r for r in tmpl.findall(v["text"]) if r not in ("filters", "period")}
        assert refs <= measures | fields, (v.get("title"), refs - measures - fields)
    flt = {f["id"] for f in spec["filters"]}
    for comp in spec["components"]:
        v = comp["visual"]
        assert set(v.get("click_filters", {})) <= flt
        assert set(v.get("ignore_filters", [])) <= flt


def test_composants_conditionnels_selon_les_colonnes():
    def titles(**kw):
        return {c["visual"].get("title") for c in sales_tracking("ds-1", **kw)["components"]}

    base, full = titles(), titles(**FULL)
    assert "Top 10 clients" not in base and "Top 10 clients" in full
    assert "Répartition par région" not in base and "Répartition par région" in full
    assert {"Quantité", "Prix moyen", "Clients actifs"} & base == set()
    assert {"Quantité", "Prix moyen", "Clients actifs"} <= full


def test_filtres_par_defaut_et_presets():
    spec = sales_tracking("ds-1", default_period="6m")
    famille, periode = spec["filters"]
    assert famille["type"] == "select" and "default" not in famille
    assert periode["type"] == "relative_period" and periode["field"] == "mois" and periode["default"] == "6m"
    assert "6m" in periode["presets"] and periode["presets"][-1] == "all"


@pytest.mark.parametrize(
    "bad",
    [{"date": "Date"}, {"amount": "1x"}, {"family": "a b"}, {"quantity": "Qte"}, {"customer": "Client"}, {"region": "x-y"}],
)
def test_noms_de_colonnes_invalides(bad):
    with pytest.raises(ValueError, match="not a valid column name"):
        sales_tracking("ds-1", **bad)


def test_periode_par_defaut_invalide():
    with pytest.raises(ValueError, match="default_period"):
        sales_tracking("ds-1", default_period="1y")


def test_colonnes_en_collision_avec_les_colonnes_calculees():
    with pytest.raises(ValueError, match="reserved"):
        sales_tracking("ds-1", family="mois")


def test_descripteur_et_build():
    assert DESCRIPTOR["id"] == "sales_tracking" and DESCRIPTOR["version"] == 1
    cols = {c["key"]: c for c in DESCRIPTOR["columns"]}
    assert set(cols) == {"date", "amount", "quantity", "family", "customer", "region"}
    assert [k for k, c in cols.items() if c["required"]] == ["date", "amount", "family"]
    assert cols["amount"]["default"] == "montant" and cols["quantity"]["default"] is None
    assert all(set(c) == {"key", "label", "type", "required", "default"} for c in cols.values())
    assert all(set(p) == {"key", "label", "type", "default", "min", "max"} for p in DESCRIPTOR["params"])
    assert build("ds-1", {"date": "date", "amount": "montant", "family": "famille"}, {}) == sales_tracking("ds-1")
    params = {p["key"]: p["default"] for p in DESCRIPTOR["params"]}
    renamed = {"date": "jour", "amount": "ca_ht", "quantity": "qte", "family": "gamme", "customer": "client_id",
               "region": "zone"}
    assert build("ds-1", renamed, params) == sales_tracking("ds-1", **RENAMED, default_period="12m")
    assert build("ds-1", {**renamed, "quantity": None, "customer": None, "region": None}, params) == sales_tracking(
        "ds-1", date="jour", amount="ca_ht", family="gamme")


def test_le_descripteur_se_charge_dans_le_catalogue():
    from apowerb.bi.spec.catalog import DashboardTemplate, TemplateColumn, TemplateParam

    template = DashboardTemplate(
        id=DESCRIPTOR["id"], label=DESCRIPTOR["label"], description=DESCRIPTOR["description"],
        columns=tuple(TemplateColumn(**c) for c in DESCRIPTOR["columns"]),
        params=tuple(TemplateParam(**p) for p in DESCRIPTOR["params"]),
        build=build,
    )
    assert template.describe() == {k: v for k, v in DESCRIPTOR.items() if k != "version"}
    columns, errors = template.clean_columns({})
    assert errors and all("obligatoire" in e for e in errors)
    filled = {c["key"]: c["default"] or None for c in DESCRIPTOR["columns"]}
    columns, errors = template.clean_columns(filled)
    params, param_errors = template.clean_params({})
    assert errors == [] and param_errors == []
    assert template.build("ds-1", columns, params) == build("ds-1", filled, params)


# ---------------------------------------------------------------------------
# Moteur réel, sans mock du calcul
# ---------------------------------------------------------------------------


def _compute(monkeypatch, kwargs, filters=None, data=None):
    import apowerb.bi.spec.sources as sources
    from apowerb.bi.spec.engine import clear_caches, compute_dashboard

    frame = _synthetic(kwargs) if data is None else data

    async def fake_load(dataset_id, owner, sheet=None):
        return frame

    clear_caches()
    monkeypatch.setattr(sources, "load_dataset", fake_load)
    dash = _dashboard(sales_tracking("ds-1", **kwargs))
    req = DashboardDataRequest(filters=filters or {})
    return dash, asyncio.run(compute_dashboard(dash, req, owner="alice@example.com")), frame


def _kpis(dash, resp):
    return {c.visual.title: resp.components[c.id].value for c in dash.components if c.visual.type == "kpi"}


def _expected(frame, kwargs, months):
    n = _names(kwargs)
    df = frame.assign(mois=frame[n["date"]].dt.to_period("M").dt.to_timestamp())
    last = df["mois"].max()
    lo = last - pd.DateOffset(months=months)
    cur = df[(df["mois"] > lo) & (df["mois"] <= last)]
    prev = df[(df["mois"] > lo - pd.DateOffset(years=1)) & (df["mois"] <= last - pd.DateOffset(years=1))]
    return df, cur, prev


def test_integration_moteur_sans_mock(monkeypatch, kwargs):
    dash, resp, frame = _compute(monkeypatch, kwargs)
    broken = {cid: r.error for cid, r in resp.components.items() if r.error}
    assert broken == {}
    kpis = _kpis(dash, resp)
    assert len(kpis) == 2 + bool(kwargs.get("quantity")) * 2 + bool(kwargs.get("customer"))
    assert all(v.value is not None for v in kpis.values()), {k: v.formatted for k, v in kpis.items()}
    print({k: v.formatted for k, v in kpis.items()})


@pytest.mark.parametrize("months", [12, 3])
def test_ca_et_evolution_comparent_a_la_meme_periode_de_l_an_dernier(monkeypatch, kwargs, months):
    n = _names(kwargs)
    dash, resp, frame = _compute(monkeypatch, kwargs, {"periode": f"{months}m"})
    _, cur, prev = _expected(frame, kwargs, months)
    kpis = _kpis(dash, resp)
    assert kpis[TITLES["ca"]].value == pytest.approx(cur[n["amount"]].sum())
    assert kpis[TITLES["evolution"]].value == pytest.approx(cur[n["amount"]].sum() / prev[n["amount"]].sum() - 1)


def test_evolution_ignore_les_mois_sans_n_moins_1(monkeypatch):
    """24 mois demandés alors que l'historique n'a que 29 mois : seuls les mois comparables comptent."""
    n = _names({})
    dash, resp, frame = _compute(monkeypatch, {}, {"periode": "all"})
    df, _, _ = _expected(frame, {}, 12)
    first = df["mois"].min()
    comparable = df[df["mois"] >= first + pd.DateOffset(years=1)]
    shifted = df.assign(mois=df["mois"] + pd.DateOffset(years=1))
    prev = shifted[shifted["mois"] <= df["mois"].max()]
    expected = comparable[n["amount"]].sum() / prev[n["amount"]].sum() - 1
    assert _kpis(dash, resp)[TITLES["evolution"]].value == pytest.approx(expected)
    assert _kpis(dash, resp)[TITLES["ca"]].value == pytest.approx(df[n["amount"]].sum())


def test_granularite_mensuelle_et_n_moins_1_dans_le_combo(monkeypatch):
    n = _names({})
    dash, resp, frame = _compute(monkeypatch, {}, {"periode": "all"})
    combo = next(c for c in dash.components if c.visual.type == "combo")
    rows = resp.components[combo.id].rows
    assert len(rows) == 29 and all(r["x"].endswith("-01") for r in rows)
    by_month = frame.groupby(frame[n["date"]].dt.to_period("M").dt.to_timestamp())[n["amount"]].sum()
    key = {s.key for s in combo.visual.series}
    ca_key, n1_key = [s.key for s in combo.visual.series]
    assert key == {ca_key, n1_key}
    for r in rows:
        month = pd.Timestamp(r["x"])
        assert r[ca_key] == pytest.approx(by_month[month])
        before = month - pd.DateOffset(years=1)
        if before in by_month.index:
            assert r[n1_key] == pytest.approx(by_month[before])
        else:
            assert r[n1_key] is None


def test_ligne_toutes_du_tableau_coherente_avec_les_kpi(monkeypatch, kwargs):
    n = _names(kwargs)
    dash, resp, frame = _compute(monkeypatch, kwargs)
    kpis = _kpis(dash, resp)
    table = next(c for c in dash.components if c.visual.type == "table")
    rows = resp.components[table.id].rows
    total = next(r for r in rows if r.get("_total"))
    familles = [r for r in rows if not r.get("_total")]
    cols = {c.label: c.key for c in table.visual.columns}
    assert len(familles) == 4
    assert total[cols["CA"]] == pytest.approx(kpis[TITLES["ca"]].value)
    assert sum(r[cols["CA"]] for r in familles) == pytest.approx(total[cols["CA"]])
    assert total[cols["Part du CA"]] == pytest.approx(1.0)
    assert sum(r[cols["Part du CA"]] for r in familles) == pytest.approx(1.0)
    assert total[cols["Évolution vs N-1"]] == pytest.approx(kpis[TITLES["evolution"]].value)
    _, cur, prev = _expected(frame, kwargs, 12)
    for r in familles:
        fam = r[table.visual.x]
        want = cur[cur[n["family"]] == fam][n["amount"]].sum() / prev[prev[n["family"]] == fam][n["amount"]].sum() - 1
        assert r[cols["Évolution vs N-1"]] == pytest.approx(want)


def test_quantite_prix_moyen_et_clients(monkeypatch):
    n = _names(FULL)
    dash, resp, frame = _compute(monkeypatch, FULL)
    kpis = _kpis(dash, resp)
    _, cur, _ = _expected(frame, FULL, 12)
    assert kpis["Quantité"].value == pytest.approx(cur[n["quantity"]].sum())
    assert kpis["Prix moyen"].value == pytest.approx(cur[n["amount"]].sum() / cur[n["quantity"]].sum())
    assert kpis["Clients actifs"].value == cur[n["customer"]].nunique()


def test_top_clients_et_regions(monkeypatch):
    n = _names(FULL)
    dash, resp, frame = _compute(monkeypatch, FULL)
    _, cur, _ = _expected(frame, FULL, 12)
    comps = {c.visual.title: resp.components[c.id].rows for c in dash.components if c.visual.title}
    top = comps["Top 10 clients"]
    assert len(top) == 10
    want = cur.groupby(n["customer"])[n["amount"]].sum().sort_values(ascending=False).head(10)
    assert [r["x"] for r in top] == list(want.index)
    regions = comps["Répartition par région"]
    assert sum(r["value"] for r in regions) == pytest.approx(cur[n["amount"]].sum())


def test_sparkline_sur_la_periode_mensuelle(monkeypatch):
    dash, resp, _ = _compute(monkeypatch, {})
    spark = _kpis(dash, resp)[TITLES["ca"]].spark
    assert spark is not None and len(spark) == 12

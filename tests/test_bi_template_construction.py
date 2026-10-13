"""Gabarit « Chantiers BTP » : une ligne par chantier et par mois (situation cumulée)."""
import asyncio

import pandas as pd
import pytest

from apowerb.bi.dashboards.core import ComponentType, Dashboard, DashboardComponent, GridPosition
from apowerb.bi.spec import engine
from apowerb.bi.spec.models import DashboardDataRequest, FilterSpec, SourceSpec, VisualSpec
from apowerb.bi.spec.templates_construction import DESCRIPTOR, build
from apowerb.bi.spec.validate import validate_spec

REQUIRED = dict(month="mois", site="chantier", sale_budget="budget_vente", cost_budget="budget_cout",
                cost_incurred="cout_engage", progress="avancement", billed_cumul="facture_cumul")
FULL = {**REQUIRED, "client": "client"}


def _dashboard(spec: dict) -> Dashboard:
    return Dashboard.create(
        "Chantiers",
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


def _situations(scale: float = 1.0) -> pd.DataFrame:
    """A (vente 1 000, coût budgété 800) : 3 situations ; B (2 000 / 1 500) : 2 situations ;
    C (500 / 400) : une seule situation à 0 % d'avancement. Les montants sont cumulés."""
    rows = [
        # mois, chantier, vente, coût budget, coût engagé (cumul), avancement, facturé (cumul), client
        ("2024-01-01", "A", 1000, 800, 200, 0.25, 300, "X"),
        ("2024-02-01", "A", 1000, 800, 450, 0.50, 500, "X"),
        ("2024-03-01", "A", 1000, 800, 700, 0.80, 700, "X"),
        ("2024-01-01", "B", 2000, 1500, 300, 0.20, 500, "Y"),
        ("2024-02-01", "B", 2000, 1500, 800, 0.50, 1300, "Y"),
        ("2024-03-01", "C", 500, 400, 0, 0.0, 0, "X"),
    ]
    df = pd.DataFrame(rows, columns=["mois", "chantier", "budget_vente", "budget_cout", "cout_engage",
                                     "avancement", "facture_cumul", "client"])
    df["mois"] = pd.to_datetime(df["mois"])
    df["avancement"] = df["avancement"] * scale
    return df


@pytest.fixture
def compute(monkeypatch):
    import apowerb.bi.spec.sources as sources
    import apowerb.bi.spec.transforms as transforms

    def run(spec, frame=None, filters=None):
        async def fake_load(dataset_id, owner, sheet=None):
            return (_situations() if frame is None else frame).copy()

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
    assert DESCRIPTOR["id"] == "construction_sites" and DESCRIPTOR["version"] == 1
    assert DESCRIPTOR["label"] and DESCRIPTOR["description"]
    cols = {c["key"]: c for c in DESCRIPTOR["columns"]}
    assert {k for k, c in cols.items() if c["required"]} == set(REQUIRED)
    assert set(cols) == set(FULL)
    for c in cols.values():
        assert set(c) == {"key", "label", "type", "required", "default"}
        assert c["type"] in ("date", "string", "number")
    assert [cols[k]["default"] for k in REQUIRED] == [
        "mois", "chantier", "budget_vente", "budget_cout", "cout_engage", "avancement", "facture_cumul"]
    assert cols["month"]["type"] == "date" and cols["site"]["type"] == "string"
    assert cols["client"]["type"] == "string" and not cols["client"]["required"]
    (param,) = DESCRIPTOR["params"]
    assert param["key"] == "drift_threshold" and param["default"] == 5
    assert set(param) == {"key", "label", "type", "default", "min", "max"}
    assert param["min"] <= param["default"] <= param["max"]


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
        build("ds-1", {**FULL, "site": "Nom du chantier"}, {})
    with pytest.raises(ValueError, match="not a valid column name"):
        build("ds-1", {**FULL, "client": "client--"}, {})


@pytest.mark.parametrize("params", [{"drift_threshold": 0}, {"drift_threshold": 500}, {"drift_threshold": 2.5},
                                    {"drift_threshold": "5"}, {"drift_threshold": True}])
def test_parametre_invalide(params):
    with pytest.raises(ValueError):
        build("ds-1", FULL, params)


# ---------------------------------------------------------------- calcul


def test_calcul_de_bout_en_bout(compute):
    dash, resp = compute(build("ds-1", FULL, {}))
    assert _errors(dash, resp) == {}
    k = _kpis(dash, resp)
    # Dernière situation de chaque chantier (jamais la somme des mois, les montants sont cumulés).
    # A : coût à terminaison 700 / 0,8 = 875, marge 125 (budget 200). B : 800 / 0,5 = 1 600, marge 400 (budget 500).
    # C n'a aucun avancement : pas d'estimation, exclu des écarts.
    assert k["marge_estimee"].value == pytest.approx(125 + 400)
    assert k["ecart_marge"].value == pytest.approx((125 + 400) - (200 + 500))
    assert k["derive_pct"].value == pytest.approx((75 + 100) / (800 + 1500))
    assert k["derive_pct"].status == "bad"  # 7,6 % > seuil de 5 %
    # Décalage : A 700 - 0,8 x 1 000 = -100 ; B 1 300 - 0,5 x 2 000 = +300 ; C 0.
    assert k["decalage_net"].value == pytest.approx(200)
    assert "300" in k["decalage_net"].secondary and "100" in k["decalage_net"].secondary
    assert k["chantiers_derive"].value == 2  # A 9,4 %, B 6,7 %, tous deux > 5 %

    chart = _by_title(dash, resp, "Marge budgétée vs estimée par chantier").rows
    rows = {r["x"]: r for r in chart}
    assert (rows["A"]["marge_budget"], rows["A"]["marge_estimee"]) == (200, pytest.approx(125))
    assert (rows["B"]["marge_budget"], rows["B"]["marge_estimee"]) == (500, pytest.approx(400))
    assert rows["C"]["marge_estimee"] is None

    drift = _by_title(dash, resp, "Dérive des coûts par mois").rows
    assert [r["derive_pct_mois"] for r in drift] == [pytest.approx(0), pytest.approx(200 / 2300), pytest.approx(75 / 800)]

    table = _by_title(dash, resp, "Chantiers par statut").rows
    by_site = {r["chantier"]: r for r in table}
    assert [r["chantier"] for r in table] == ["A", "B", "C"]  # dérive décroissante, non évaluable en dernier
    assert by_site["A"]["statut"] == "En dérive" and by_site["B"]["statut"] == "En dérive"
    assert by_site["C"]["statut"] == "Non évaluable"
    assert by_site["A"]["derive_pct"] == pytest.approx(75 / 800)
    assert by_site["A"]["_status_derive_pct"] == "bad"
    assert by_site["A"]["avancement_actuel"] == pytest.approx(0.8)
    assert by_site["A"]["decalage_net"] == pytest.approx(-100) and by_site["B"]["decalage_net"] == pytest.approx(300)
    assert by_site["A"]["client"] == "X"
    text = next(resp.components[c.id].text for c in dash.components if c.visual.type == "text")
    assert "3" in text


def test_avancement_en_pourcentage_donne_les_memes_resultats(compute):
    dash, resp = compute(build("ds-1", FULL, {}), frame=_situations(scale=100))
    k = _kpis(dash, resp)
    assert k["marge_estimee"].value == pytest.approx(525)
    assert k["derive_pct"].value == pytest.approx(175 / 2300)


def test_seuil_de_derive_reglable(compute):
    dash, resp = compute(build("ds-1", FULL, {"drift_threshold": 8}))
    k = _kpis(dash, resp)
    assert k["chantiers_derive"].value == 1  # seul A (9,4 %) dépasse 8 %
    assert k["derive_pct"].status == "warn"  # 7,6 % : positif mais sous le seuil
    table = {r["chantier"]: r for r in _by_title(dash, resp, "Chantiers par statut").rows}
    assert table["A"]["statut"] == "En dérive" and table["B"]["statut"] == "À surveiller"
    dash, resp = compute(build("ds-1", FULL, {"drift_threshold": 10}))
    assert _kpis(dash, resp)["chantiers_derive"].value == 0


def test_chantier_sous_budget(compute):
    df = _situations()
    df.loc[(df["chantier"] == "B") & (df["mois"] == "2024-02-01"), "cout_engage"] = 600  # 600 / 0,5 = 1 200 < 1 500
    dash, resp = compute(build("ds-1", FULL, {}), frame=df)
    table = {r["chantier"]: r for r in _by_title(dash, resp, "Chantiers par statut").rows}
    assert table["B"]["statut"] == "Maîtrisé"
    assert table["B"]["derive_pct"] == pytest.approx(-300 / 1500)


def test_filtre_chantier_et_client(compute):
    spec = build("ds-1", FULL, {})
    dash, resp = compute(spec, filters={"chantier": "A"})
    k = _kpis(dash, resp)
    assert k["marge_estimee"].value == pytest.approx(125) and k["chantiers_derive"].value == 1
    assert "suivis : 1" in k["chantiers_derive"].secondary
    dash, resp = compute(spec, filters={"client": "X"})  # A et C
    k = _kpis(dash, resp)
    assert k["marge_estimee"].value == pytest.approx(125) and k["chantiers_derive"].value == 1
    assert "suivis : 2" in k["chantiers_derive"].secondary


# ---------------------------------------------------------------- colonne optionnelle absente


def test_sans_client(compute):
    spec = build("ds-1", REQUIRED, {})
    dash, resp = compute(spec)
    assert _errors(dash, resp) == {}
    assert [f.id for f in dash.filters] == ["periode", "chantier"]
    table = next(c.visual for c in dash.components if c.visual.type == "table")
    assert "client" not in [c.key for c in table.columns]
    assert _kpis(dash, resp)["marge_estimee"].value == pytest.approx(525)

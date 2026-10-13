"""Gabarit « E-commerce » : une ligne par commande (+ dépenses pub), calcul de bout en bout."""
import asyncio

import pandas as pd
import pytest

from apowerb.bi.dashboards.core import ComponentType, Dashboard, DashboardComponent, GridPosition
from apowerb.bi.spec import engine
from apowerb.bi.spec.models import DashboardDataRequest, FilterSpec, SourceSpec, VisualSpec
from apowerb.bi.spec.templates_ecommerce import DESCRIPTOR, build
from apowerb.bi.spec.validate import validate_spec

FULL = dict(
    date="date", order_id="commande_id", customer_id="client_id", amount="montant",
    product_cost="cout_produits", channel="canal", ad_spend="depense_pub",
    new_customer="nouveau_client", status="statut",
)
REQUIRED = dict(date="date", order_id="commande_id", amount="montant")


def _dashboard(spec: dict) -> Dashboard:
    return Dashboard.create(
        "E-commerce",
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


def _orders() -> pd.DataFrame:
    """Semaine 1 : o1-o3 (+ 100 de pub) ; semaine 2 : o4, o6 (+ 200 de pub).
    o5 est annulée et o7 remboursée : exclues. Les dépenses pub sont des lignes sans commande."""
    rows = [
        # date, commande, client, montant, coût, canal, pub, nouveau, statut
        ("2024-01-02", "o1", "c1", 100, 40, "Google", None, 1, "payée"),
        ("2024-01-03", "o2", "c2", 200, 100, "Meta", None, 1, "payée"),
        ("2024-01-04", "o3", "c1", 50, 20, "Google", None, 0, "payée"),
        ("2024-01-03", None, None, None, None, "Google", 100, None, None),
        ("2024-01-09", "o4", "c3", 150, 60, "Meta", None, 1, "payée"),
        ("2024-01-10", "o5", "c2", 100, 50, "Google", None, 0, "annulée"),
        ("2024-01-11", "o6", "c1", 300, 120, "Google", None, 0, "Payée"),
        ("2024-01-12", "o7", "c4", 80, 30, "Meta", None, 0, "remboursée"),
        ("2024-01-10", None, None, None, None, "Meta", 200, None, None),
    ]
    df = pd.DataFrame(rows, columns=["date", "commande_id", "client_id", "montant", "cout_produits", "canal",
                                     "depense_pub", "nouveau_client", "statut"])
    df["date"] = pd.to_datetime(df["date"])
    return df


@pytest.fixture
def compute(monkeypatch):
    import apowerb.bi.spec.sources as sources
    import apowerb.bi.spec.transforms as transforms

    def run(spec, frame=None, filters=None):
        async def fake_load(dataset_id, owner, sheet=None):
            return (_orders() if frame is None else frame).copy()

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


# ---------------------------------------------------------------- descripteur


def test_descriptor_conforme():
    assert DESCRIPTOR["id"] == "ecommerce" and DESCRIPTOR["version"] == 1
    assert DESCRIPTOR["label"] and DESCRIPTOR["description"]
    cols = {c["key"]: c for c in DESCRIPTOR["columns"]}
    assert {k for k, c in cols.items() if c["required"]} == {"date", "order_id", "amount"}
    assert set(cols) == set(FULL)
    for c in cols.values():
        assert set(c) == {"key", "label", "type", "required", "default"}
        assert c["type"] in ("date", "string", "number")
        assert (c["default"] is None) or isinstance(c["default"], str)
    assert [cols[k]["default"] for k in ("date", "order_id", "amount")] == ["date", "commande_id", "montant"]
    assert cols["date"]["type"] == "date" and cols["amount"]["type"] == "number"
    assert cols["order_id"]["type"] == "string" and cols["new_customer"]["type"] == "number"
    (param,) = DESCRIPTOR["params"]
    assert param["key"] == "roas_threshold" and param["default"] == 2
    assert param["min"] <= param["default"] <= param["max"]
    assert set(param) == {"key", "label", "type", "default", "min", "max"}


# ---------------------------------------------------------------- structure


@pytest.mark.parametrize("columns", [FULL, REQUIRED, {**REQUIRED, "ad_spend": "depense_pub"},
                                     {**REQUIRED, "customer_id": "client_id", "new_customer": "nouveau_client"}])
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


@pytest.mark.parametrize("missing", ["date", "order_id", "amount"])
@pytest.mark.parametrize("blank", [None, ""])
def test_colonne_obligatoire_manquante(missing, blank):
    with pytest.raises(ValueError):
        build("ds-1", {**FULL, missing: blank}, {})


def test_dataset_obligatoire_et_noms_valides():
    with pytest.raises(ValueError):
        build("", FULL, {})
    with pytest.raises(ValueError, match="not a valid column name"):
        build("ds-1", {**FULL, "amount": "Montant TTC"}, {})
    with pytest.raises(ValueError, match="not a valid column name"):
        build("ds-1", {**FULL, "channel": "canal; DROP"}, {})


@pytest.mark.parametrize("params", [{"roas_threshold": 0}, {"roas_threshold": 99}, {"roas_threshold": "2"},
                                    {"roas_threshold": True}])
def test_parametre_invalide(params):
    with pytest.raises(ValueError):
        build("ds-1", FULL, params)


def test_seuil_roas_reglable_et_defaut():
    def roas_line(params):
        spec = build("ds-1", FULL, params)
        measure = next(m for m in spec["sources"][0]["measures"] if m["name"] == "roas")
        chart = next(c["visual"] for c in spec["components"] if c["visual"].get("title") == "ROAS par semaine")
        return measure["thresholds"], chart["reference_lines"][0]["value"]

    assert roas_line({}) == roas_line({"roas_threshold": 2})
    thr, line = roas_line({"roas_threshold": 3})
    assert thr["good"] == 3 and line == 3


# ---------------------------------------------------------------- calcul


def test_calcul_de_bout_en_bout(compute):
    spec = build("ds-1", FULL, {})
    dash, resp = compute(spec)
    assert _errors(dash, resp) == {}
    k = _kpis(dash, resp)
    # commandes valides : o1 o2 o3 o4 o6 (o5 annulée, o7 remboursée exclues)
    assert k["ca"].value == 800
    assert k["commandes"].value == 5
    assert k["panier_moyen"].value == pytest.approx(160)
    # marge de contribution = CA - coûts produits (160 + 180) - pub (300)
    assert k["marge_contribution"].value == pytest.approx(800 - 340 - 300)
    assert k["roas"].value == pytest.approx(800 / 300)
    assert k["roas"].status == "good"  # 2,67 >= 2
    # nouveaux clients : c1, c2, c3 -> CAC = 300 / 3
    assert k["cac"].value == pytest.approx(100)
    # clients actifs c1 (3 commandes), c2, c3 -> 1 récurrent sur 3
    assert k["clients_recurrents"].value == pytest.approx(1 / 3)

    weekly = _by_title(dash, resp, "CA et commandes par semaine").rows
    assert [(r["ca"], r["commandes"]) for r in weekly] == [(350, 3), (450, 2)]
    roas = _by_title(dash, resp, "ROAS par semaine")
    assert [r["roas"] for r in roas.rows] == [pytest.approx(3.5), pytest.approx(2.25)]
    assert roas.reference_lines[0]["value"] == 2
    by_channel = {r["x"]: r["ca"] for r in _by_title(dash, resp, "CA par canal").rows if r["x"] is not None}
    assert by_channel == {"Google": 450, "Meta": 350}
    top = _by_title(dash, resp, "Top clients").rows
    assert [(r["x"], r["ca"]) for r in top] == [("c1", 450), ("c2", 200), ("c3", 150)]
    text = next(resp.components[c.id].text for c in dash.components if c.visual.type == "text")
    assert "800" in text and "5" in text


def test_roas_sous_le_seuil_declenche_l_alerte(compute):
    dash, resp = compute(build("ds-1", FULL, {"roas_threshold": 3}))
    assert _kpis(dash, resp)["roas"].status == "bad"  # 2,67 < 3


def test_sans_colonne_statut_toutes_les_commandes_comptent(compute):
    cols = {k: v for k, v in FULL.items() if k != "status"}
    dash, resp = compute(build("ds-1", cols, {}))
    k = _kpis(dash, resp)
    assert k["commandes"].value == 7
    assert k["ca"].value == 800 + 100 + 80


def test_statut_aux_variantes_courantes(compute):
    df = _orders()
    df.loc[df["commande_id"] == "o1", "statut"] = "cancelled"
    df.loc[df["commande_id"] == "o2", "statut"] = "Refunded"
    df.loc[df["commande_id"] == "o3", "statut"] = "Annulée"
    dash, resp = compute(build("ds-1", FULL, {}), frame=df)
    assert _kpis(dash, resp)["commandes"].value == 2  # o4, o6


def test_filtre_canal(compute):
    dash, resp = compute(build("ds-1", FULL, {}), filters={"canal": "Meta"})
    k = _kpis(dash, resp)
    assert k["ca"].value == 350  # o2 + o4
    assert k["roas"].value == pytest.approx(350 / 200)  # pub Meta : 200


# ---------------------------------------------------------------- colonnes optionnelles absentes


def _titles(dash):
    return {c.visual.title for c in dash.components if c.visual.title}


def test_colonnes_obligatoires_seules(compute):
    spec = build("ds-1", REQUIRED, {})
    dash, resp = compute(spec)
    assert _errors(dash, resp) == {}
    assert set(_kpis(dash, resp)) == {"ca", "commandes", "panier_moyen"}
    titles = _titles(dash)
    assert {"CA et commandes par semaine", "Définitions"} <= titles
    assert not titles & {"ROAS par semaine", "CA par canal", "Top clients"}
    assert [f.id for f in dash.filters] == ["periode"]
    # sans statut, les 7 commandes comptent
    assert _kpis(dash, resp)["commandes"].value == 7


def test_pub_sans_cout_ni_nouveaux_clients(compute):
    dash, resp = compute(build("ds-1", {**REQUIRED, "ad_spend": "depense_pub"}, {}))
    assert _errors(dash, resp) == {}
    k = _kpis(dash, resp)
    assert "roas" in k and "cac" not in k and "marge_contribution" not in k and "clients_recurrents" not in k


def test_couts_sans_pub_la_marge_ignore_la_pub(compute):
    dash, resp = compute(build("ds-1", {**REQUIRED, "product_cost": "cout_produits", "status": "statut"}, {}))
    assert _errors(dash, resp) == {}
    k = _kpis(dash, resp)
    assert k["marge_contribution"].value == pytest.approx(800 - 340)
    assert "roas" not in k


def test_clients_sans_drapeau_nouveau_client(compute):
    dash, resp = compute(build("ds-1", {**REQUIRED, "customer_id": "client_id", "status": "statut"}, {}))
    assert _errors(dash, resp) == {}
    k = _kpis(dash, resp)
    assert k["clients_recurrents"].value == pytest.approx(1 / 3)
    assert "cac" not in k
    assert "Top clients" in _titles(dash)

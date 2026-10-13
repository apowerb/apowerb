"""Gabarit « Services B2B » : descripteur, spec valide, grille sans chevauchement, calcul de bout en bout
sur un jeu synthétique construit à la main (valeurs attendues calculées à la main), colonnes
optionnelles absentes, colonne obligatoire manquante."""
import asyncio
import re

import pandas as pd
import pytest

from apowerb.bi.dashboards.core import ComponentType, Dashboard, DashboardComponent, GridPosition
from apowerb.bi.spec.models import DashboardDataRequest, FilterSpec, SourceSpec, VisualSpec
from apowerb.bi.spec.templates_services import DESCRIPTOR, build, services_b2b
from apowerb.bi.spec.validate import validate_spec

FULL = dict(client="client", project="projet", day_rate="tjm", invoiced="montant_facture")
PERIOD = "4w"


def _dashboard(spec: dict) -> Dashboard:
    return Dashboard.create(
        "Services",
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


def _data(billable=lambda yes: yes) -> pd.DataFrame:
    """6 semaines (lundis du 06/10 au 10/11/2025), 2 consultants.

    Chaque semaine, Ana : Acme/Alpha 24 h facturables (TJM 700, 2 400 facturés) + Interne/Avant-vente 6 h non
    facturables, 35 h disponibles répétées sur ses deux lignes.
    Ben : Beta/Bravo 21 h facturables (TJM 500, 1 500 facturés les 4 premières semaines, rien ensuite) + Interne/
    Formation 7 h non facturables, 35 h disponibles saisies une seule fois (0 sur l'autre ligne).
    """
    rows = []
    for w in range(6):
        d = pd.Timestamp("2025-10-06") + pd.Timedelta(weeks=w)
        rows += [
            dict(date=d, consultant="Ana", client="Acme", projet="Alpha", heures=24, facturable=billable(True),
                 heures_disponibles=35, tjm=700.0, montant_facture=2400.0),
            dict(date=d, consultant="Ana", client="Interne", projet="Avant-vente", heures=6,
                 facturable=billable(False), heures_disponibles=35, tjm=None, montant_facture=None),
            dict(date=d, consultant="Ben", client="Beta", projet="Bravo", heures=21, facturable=billable(True),
                 heures_disponibles=35, tjm=500.0, montant_facture=1500.0 if w <= 3 else None),
            dict(date=d, consultant="Ben", client="Interne", projet="Formation", heures=7,
                 facturable=billable(False), heures_disponibles=0, tjm=None, montant_facture=None),
        ]
    return pd.DataFrame(rows)


def _compute(monkeypatch, spec: dict, df: pd.DataFrame, filters: dict | None = None):
    import apowerb.bi.spec.sources as sources
    import apowerb.bi.spec.transforms as transforms
    from apowerb.bi.spec.engine import compute_dashboard

    async def fake_load(dataset_id, owner, sheet=None):
        return df

    transforms.clear_cache()
    sources.clear_cache()
    monkeypatch.setattr(sources, "load_dataset", fake_load)
    dash = _dashboard(spec)
    resp = asyncio.run(compute_dashboard(dash, DashboardDataRequest(filters=filters or {}), owner="alice@example.com"))
    return dash, resp


def _kpis(dash, resp) -> dict:
    return {c.visual.title: resp.components[c.id].value for c in dash.components if c.visual.type == "kpi"}


def _component(dash, resp, title):
    comp = next(c for c in dash.components if c.visual.title == title)
    return comp.visual, resp.components[comp.id]


def _no_errors(resp):
    assert {cid: r.error for cid, r in resp.components.items() if r.error} == {}


# ---------------------------------------------------------------------------
# Contrat : descripteur, build, spec
# ---------------------------------------------------------------------------


def test_descripteur():
    assert {"id", "label", "description", "version", "columns", "params"} == set(DESCRIPTOR)
    assert DESCRIPTOR["id"] == "services_b2b" and DESCRIPTOR["version"] == 1
    cols = {c["key"]: c for c in DESCRIPTOR["columns"]}
    assert list(cols) == [
        "date", "consultant", "client", "projet", "heures", "facturable", "heures_disponibles", "tjm",
        "montant_facture",
    ]
    assert all(set(c) == {"key", "label", "type", "required", "default"} for c in cols.values())
    assert {k for k, c in cols.items() if c["required"]} == {
        "date", "consultant", "heures", "facturable", "heures_disponibles",
    }
    assert cols["date"]["type"] == "date" and cols["consultant"]["type"] == "string"
    assert cols["heures"]["type"] == "number" and cols["tjm"]["type"] == "number"
    assert all(c["type"] in ("date", "string", "number") for c in cols.values())
    assert all(c["default"] == c["default"].lower() for c in cols.values())
    params = {p["key"]: p for p in DESCRIPTOR["params"]}
    assert len(params) == 2 and all(set(p) == {"key", "label", "type", "default", "min", "max"} for p in params.values())
    assert all(p["type"] == "integer" and p["min"] <= p["default"] <= p["max"] for p in params.values())


def test_spec_valide_et_sans_chevauchement():
    for kwargs in ({}, FULL, {"client": "client"}, {"day_rate": "tjm"}, {"invoiced": "montant_facture"}):
        spec = services_b2b("ds-1", **kwargs)
        assert validate_spec(_dashboard(spec)) == []
        assert spec["sources"][0]["kind"] == "dataset" and spec["sources"][0]["dataset_id"] == "ds-1"
        taken: dict[tuple[int, int], int] = {}
        for idx, comp in enumerate(spec["components"]):
            p = comp["position"]
            assert 0 <= p["col"] and p["col"] + p["width"] <= 12, p
            for r in range(p["row"], p["row"] + p["height"]):
                for c in range(p["col"], p["col"] + p["width"]):
                    assert (r, c) not in taken, f"composant {idx} chevauche {taken[(r, c)]}"
                    taken[(r, c)] = idx


def test_chaque_mesure_referencee_existe():
    spec = services_b2b("ds-1", **FULL)
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
        refs |= {c["key"] for c in v.get("columns", [])} - {"consultant"}
        if v["type"] == "text":
            refs |= {r for r in tmpl.findall(v["text"]) if r not in ("filters", "period")}
        assert refs <= measures | fields, (v.get("title"), refs - measures - fields)


def test_cibles_par_defaut_et_parametres():
    spec = services_b2b("ds-1", **FULL)
    m = {x["name"]: x for x in spec["sources"][0]["measures"]}
    assert m["taux_facturable"]["thresholds"] == {"direction": "higher_is_better", "good": 0.75, "warn": 0.65}
    assert m["occupation"]["thresholds"] == {"direction": "higher_is_better", "good": 0.85, "warn": 0.75}
    line = lambda s, title: next(  # noqa: E731
        c["visual"]["reference_lines"] for c in s["components"] if c["visual"].get("title") == title)
    assert [r["value"] for r in line(spec, "Taux facturable par semaine")] == [0.75]
    assert [r["value"] for r in line(spec, "Occupation par consultant")] == [0.85]
    columns = {c["key"]: c["default"] for c in DESCRIPTOR["columns"]}
    custom = build("ds-1", columns, {"billable_target": 80, "occupancy_target": 90})
    m2 = {x["name"]: x for x in custom["sources"][0]["measures"]}
    assert m2["taux_facturable"]["thresholds"]["good"] == 0.8 and m2["taux_facturable"]["thresholds"]["warn"] == 0.7
    assert m2["occupation"]["thresholds"]["good"] == 0.9 and m2["occupation"]["thresholds"]["warn"] == 0.8
    assert [r["value"] for r in line(custom, "Taux facturable par semaine")] == [0.8]
    assert [r["value"] for r in line(custom, "Occupation par consultant")] == [0.9]


def test_composants_conditionnels():
    titles = lambda s: {c["visual"].get("title") for c in s["components"]}  # noqa: E731
    base = titles(services_b2b("ds-1"))
    assert {"Taux d'occupation", "Taux facturable", "Taux facturable par semaine", "Occupation par consultant",
            "Activité par consultant"} <= base
    assert not base & {"TJM moyen", "Top clients", "CA par consultant", "Reste à facturer"}
    only_rate = titles(services_b2b("ds-1", day_rate="tjm"))
    assert {"TJM moyen", "CA par consultant", "CA valorisé au TJM"} <= only_rate and "Reste à facturer" not in only_rate
    only_amount = titles(services_b2b("ds-1", invoiced="montant_facture"))
    assert {"TJM moyen", "CA facturé"} <= only_amount and "Reste à facturer" not in only_amount
    full = titles(services_b2b("ds-1", **FULL))
    assert {"TJM moyen", "CA facturé", "CA par consultant", "Reste à facturer", "Top clients"} <= full
    assert {f["id"] for f in services_b2b("ds-1")["filters"]} == {"periode", "consultant"}
    assert {f["id"] for f in services_b2b("ds-1", **FULL)["filters"]} == {"periode", "consultant", "client", "projet"}


@pytest.mark.parametrize(
    "kwargs",
    [{"date": "Date"}, {"consultant": "a b"}, {"hours": "1h"}, {"billable": "fact-able"}, {"client": "Client"},
     {"day_rate": "TJM"}],
)
def test_noms_de_colonnes_invalides(kwargs):
    with pytest.raises(ValueError, match="not a valid column name"):
        services_b2b("ds-1", **kwargs)


def test_cibles_hors_bornes():
    with pytest.raises(ValueError, match="billable_target"):
        services_b2b("ds-1", billable_target=0)
    with pytest.raises(ValueError, match="occupancy_target"):
        services_b2b("ds-1", occupancy_target=101)


def test_build_appelle_le_gabarit_par_mots_cles():
    columns = {c["key"]: c["default"] for c in DESCRIPTOR["columns"]}
    assert build("ds-1", columns, {}) == services_b2b("ds-1", **FULL)
    columns.update(tjm="", montant_facture=None, consultant="conseiller")
    assert build("ds-1", columns, {}) == services_b2b("ds-1", consultant="conseiller", client="client",
                                                      project="projet")


@pytest.mark.parametrize("missing", ["date", "consultant", "heures", "facturable", "heures_disponibles"])
def test_build_colonne_obligatoire_manquante(missing):
    columns = {c["key"]: c["default"] for c in DESCRIPTOR["columns"]}
    columns[missing] = None
    with pytest.raises(ValueError, match="not a valid column name"):
        build("ds-1", columns, {})


# ---------------------------------------------------------------------------
# Intégration sans mock du moteur, valeurs calculées à la main
# ---------------------------------------------------------------------------
# Fenêtre 4 semaines (les 4 dernières, du 20/10 au 10/11) :
#   heures (24 + 6 + 21 + 7) x 4 = 232 ; disponibles 70 x 4 = 280 (compté une fois par consultant et jour)
#   occupation 232 / 280 ; facturables (24 + 21) x 4 = 180 -> 180 / 232 ; 180 / 7 jours
#   TJM pondéré (24 x 700 + 21 x 500) x 4 / 180 = 109 200 / 180
#   CA facturé : Ana 4 x 2 400 + Ben 2 x 1 500 = 12 600 ; valorisé au TJM 9 600 + 6 000 = 15 600
#   reste à facturer 15 600 - 12 600 = 3 000
OCCUPATION = 232 / 280
FACTURABLE = 180 / 232
TJM = 109_200 / 180
CA = 12_600


def _by_kpi(monkeypatch, kwargs=None, df=None, **extra):
    spec = services_b2b("ds-1", default_period=PERIOD, **(FULL if kwargs is None else kwargs))
    dash, resp = _compute(monkeypatch, spec, _data() if df is None else df, **extra)
    _no_errors(resp)
    return dash, resp, _kpis(dash, resp)


def test_integration_kpi_calcules_a_la_main(monkeypatch):
    dash, resp, kpis = _by_kpi(monkeypatch)
    assert list(kpis) == [
        "Taux d'occupation", "Taux facturable", "TJM moyen", "CA facturé", "CA par consultant", "Reste à facturer",
    ]
    assert kpis["Taux d'occupation"].value == pytest.approx(OCCUPATION)
    assert kpis["Taux d'occupation"].status == "warn"  # 82,9 % : sous la cible 85 %, au-dessus de 75 %
    assert kpis["Taux facturable"].value == pytest.approx(FACTURABLE)
    assert kpis["Taux facturable"].status == "good"  # 77,6 % >= 75 %
    assert kpis["TJM moyen"].value == pytest.approx(TJM)
    assert kpis["CA facturé"].value == CA
    assert kpis["CA par consultant"].value == CA / 2
    assert kpis["Reste à facturer"].value == pytest.approx(3_000)
    assert "25,7" in kpis["Taux facturable"].secondary  # 180 / 7 jours
    assert kpis["Taux facturable"].spark is not None and len(kpis["Taux facturable"].spark) == 4


def test_fenetre_exclut_les_premieres_semaines(monkeypatch):
    spec = services_b2b("ds-1", default_period="all", **FULL)
    dash, resp = _compute(monkeypatch, spec, _data())
    _no_errors(resp)
    # 6 semaines : 58 h x 6 = 348 h passées, 70 x 6 = 420 h disponibles
    assert _kpis(dash, resp)["Taux d'occupation"].value == pytest.approx(348 / 420)


def test_graphiques(monkeypatch):
    dash, resp, kpis = _by_kpi(monkeypatch)
    _, semaine = _component(dash, resp, "Taux facturable par semaine")
    assert len(semaine.rows) == 4
    assert semaine.rows[0]["x"].startswith("2025-10-20") and semaine.rows[-1]["x"].startswith("2025-11-10")
    assert all(r["taux_facturable"] == pytest.approx(45 / 58) for r in semaine.rows)
    assert semaine.reference_lines[0]["value"] == 0.75

    _, occupation = _component(dash, resp, "Occupation par consultant")
    assert [(r["x"], round(r["occupation"], 6)) for r in occupation.rows] == [
        ("Ben", round(112 / 140, 6)), ("Ana", round(120 / 140, 6)),
    ]  # les moins occupés d'abord
    assert occupation.rows[0]["_color_occupation"] == "warn"  # 80 % : entre 75 % et 85 %
    assert occupation.rows[1]["_color_occupation"] == "good"

    _, clients = _component(dash, resp, "Top clients")
    assert {r["x"]: r["ca"] for r in clients.rows} == {"Acme": 9_600, "Beta": 3_000, "Interne": 0}
    assert [r["x"] for r in clients.rows][:2] == ["Acme", "Beta"]


def test_tableau_par_consultant_et_ligne_toutes(monkeypatch):
    dash, resp, kpis = _by_kpi(monkeypatch)
    _, table = _component(dash, resp, "Activité par consultant")
    rows = {r["consultant"]: r for r in table.rows if not r.get("_total")}
    assert rows["Ana"]["heures_total"] == 120 and rows["Ben"]["heures_total"] == 112
    assert rows["Ana"]["taux_facturable"] == pytest.approx(96 / 120)
    assert rows["Ben"]["taux_facturable"] == pytest.approx(84 / 112)
    assert rows["Ana"]["ca"] == 9_600 and rows["Ben"]["ca"] == 3_000
    assert rows["Ana"]["tjm_moyen"] == pytest.approx(700) and rows["Ben"]["tjm_moyen"] == pytest.approx(500)
    assert rows["Ana"]["reste_a_facturer"] == 0 and rows["Ben"]["reste_a_facturer"] == pytest.approx(3_000)
    total = next(r for r in table.rows if r.get("_total"))
    assert total["occupation"] == pytest.approx(kpis["Taux d'occupation"].value)
    assert total["taux_facturable"] == pytest.approx(kpis["Taux facturable"].value)
    assert total["ca"] == kpis["CA facturé"].value
    assert total["reste_a_facturer"] == pytest.approx(kpis["Reste à facturer"].value)


@pytest.mark.parametrize(
    "billable",
    [lambda yes: yes, lambda yes: 1 if yes else 0, lambda yes: 1.0 if yes else 0.0,
     lambda yes: "oui" if yes else "non", lambda yes: "True" if yes else "False"],
    ids=["booleen", "entier", "decimal", "oui_non", "texte_booleen"],
)
def test_facturable_booleen_ou_0_1(monkeypatch, billable):
    _, _, kpis = _by_kpi(monkeypatch, df=_data(billable))
    assert kpis["Taux facturable"].value == pytest.approx(FACTURABLE)
    assert kpis["TJM moyen"].value == pytest.approx(TJM)
    assert kpis["Reste à facturer"].value == pytest.approx(3_000)


def test_heures_disponibles_repetees_ou_saisies_une_fois(monkeypatch):
    # Ana répète ses 35 h sur deux lignes, Ben les saisit une fois : 35 h par consultant et semaine dans les deux cas.
    _, resp, kpis = _by_kpi(monkeypatch)
    assert kpis["Taux d'occupation"].value == pytest.approx(232 / 280)
    heures = _data()
    heures["heures_disponibles"] = 35
    _, _, again = _by_kpi(monkeypatch, df=heures)
    assert again["Taux d'occupation"].value == pytest.approx(232 / 280)


def test_cibles_personnalisees_changent_le_statut(monkeypatch):
    spec = services_b2b("ds-1", default_period=PERIOD, billable_target=80, occupancy_target=90, **FULL)
    dash, resp = _compute(monkeypatch, spec, _data())
    kpis = _kpis(dash, resp)
    assert kpis["Taux facturable"].status == "warn"  # 77,6 % : sous 80 %, au-dessus de 70 %
    assert kpis["Taux d'occupation"].status == "warn"  # 82,9 % : sous 90 %, au-dessus de 80 %


def test_filtre_client(monkeypatch):
    spec = services_b2b("ds-1", default_period=PERIOD, **FULL)
    dash, resp = _compute(monkeypatch, spec, _data(), {"client": "Acme"})
    _no_errors(resp)
    kpis = _kpis(dash, resp)
    assert kpis["CA facturé"].value == 9_600
    assert kpis["Taux facturable"].value == 1.0
    assert kpis["TJM moyen"].value == pytest.approx(700)


def test_sans_colonnes_optionnelles(monkeypatch):
    df = _data().drop(columns=["client", "projet", "tjm", "montant_facture"])
    dash, resp, kpis = _by_kpi(monkeypatch, kwargs={}, df=df)
    assert list(kpis) == ["Taux d'occupation", "Taux facturable"]
    assert kpis["Taux d'occupation"].value == pytest.approx(OCCUPATION)
    assert kpis["Taux facturable"].value == pytest.approx(FACTURABLE)
    titles = {c.visual.title for c in dash.components}
    assert "Top clients" not in titles and "Activité par consultant" in titles
    _, table = _component(dash, resp, "Activité par consultant")
    assert {r["consultant"] for r in table.rows if not r.get("_total")} == {"Ana", "Ben"}


def test_seulement_le_tjm(monkeypatch):
    df = _data().drop(columns=["montant_facture"])
    dash, resp, kpis = _by_kpi(monkeypatch, kwargs=dict(client="client", day_rate="tjm"), df=df)
    assert list(kpis) == ["Taux d'occupation", "Taux facturable", "TJM moyen", "CA valorisé au TJM",
                          "CA par consultant"]
    assert kpis["TJM moyen"].value == pytest.approx(TJM)
    assert kpis["CA valorisé au TJM"].value == pytest.approx(15_600)
    assert kpis["CA par consultant"].value == pytest.approx(7_800)
    _, clients = _component(dash, resp, "Top clients")
    assert clients.rows[0]["x"] == "Acme" and clients.rows[0]["ca"] == pytest.approx(9_600)


def test_seulement_le_montant_facture(monkeypatch):
    df = _data().drop(columns=["tjm"])
    _, _, kpis = _by_kpi(monkeypatch, kwargs=dict(invoiced="montant_facture"), df=df)
    assert list(kpis) == ["Taux d'occupation", "Taux facturable", "TJM moyen", "CA facturé", "CA par consultant"]
    assert kpis["CA facturé"].value == CA
    # jours facturables des seules lignes facturées : Ana 4 x 24 h + Ben 2 x 21 h = 138 h -> 19,71 jours
    assert kpis["TJM moyen"].value == pytest.approx(CA * 7 / 138)


def test_aucune_heure_facturable_ne_casse_pas(monkeypatch):
    df = _data(lambda yes: False)
    _, _, kpis = _by_kpi(monkeypatch, df=df)
    assert kpis["Taux facturable"].value == 0 and kpis["Taux facturable"].status == "bad"
    assert kpis["Reste à facturer"].value == 0


def test_colonne_nommee_comme_le_regroupement_temporel(monkeypatch):
    df = _data().rename(columns={"date": "semaine"})
    spec = services_b2b("ds-1", date="semaine", default_period=PERIOD, **FULL)
    dash, resp = _compute(monkeypatch, spec, df)
    _no_errors(resp)
    assert _kpis(dash, resp)["Taux d'occupation"].value == pytest.approx(OCCUPATION)

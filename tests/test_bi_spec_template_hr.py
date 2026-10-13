"""Gabarit « RH » : descripteur, spec valide, grille sans chevauchement, calcul de bout en bout
sur un jeu synthétique construit à la main (valeurs attendues calculées à la main), colonnes
optionnelles absentes, colonne obligatoire manquante."""
import asyncio
import re

import pandas as pd
import pytest

from apowerb.bi.dashboards.core import ComponentType, Dashboard, DashboardComponent, GridPosition
from apowerb.bi.spec.models import DashboardDataRequest, FilterSpec, SourceSpec, VisualSpec
from apowerb.bi.spec.templates_hr import DESCRIPTOR, build, hr_people
from apowerb.bi.spec.validate import validate_spec

FULL = dict(
    department="service", hire_date="entree", exit_date="sortie", absence_reason="motif_absence",
)
KPI_TITLES = ["Taux d'absentéisme", "Turnover", "Effectif", "Masse salariale", "Coût moyen par salarié"]


def _dashboard(spec: dict) -> Dashboard:
    return Dashboard.create(
        "RH",
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


def _rows(name, service, months, theo, absence, motif, payroll, entree, sortie):
    out = []
    for i, m in enumerate(months):
        a = absence[i] if isinstance(absence, list) else absence
        mo = motif[i] if isinstance(motif, list) else motif
        out.append({
            "mois": m, "salarie": name, "service": service, "heures_theoriques": theo, "heures_absence": a,
            "masse_salariale": payroll, "entree": entree, "sortie": sortie, "motif_absence": mo,
        })
    return out


def _data() -> pd.DataFrame:
    """14 mois (2025-01 à 2026-02) ; la fenêtre 12 mois par défaut va de 2025-03 à 2026-02.

    A, B (Compta) présents tout le temps ; C (Prod) part le 15/09/2025 ; D (Prod) entre le 10/11/2025.
    """
    months = list(pd.date_range("2025-01-01", periods=14, freq="MS"))
    nat = pd.NaT
    rows = []
    rows += _rows("A", "Compta", months, 150, 6, "Maladie", 3000, pd.Timestamp("2020-03-01"), nat)
    rows += _rows("B", "Compta", months, 150, 15, "Maladie", 4000, pd.Timestamp("2021-06-01"), nat)
    rows += _rows("C", "Prod", months[:9], 150, 7.5, "Congé sans solde", 2500, pd.Timestamp("2023-01-02"),
                  pd.Timestamp("2025-09-15"))
    rows += _rows("D", "Prod", months[10:], 150, [10, 20, 0, 0], ["Accident du travail", None, None, None], 2000,
                  pd.Timestamp("2025-11-10"), nat)
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
    assert DESCRIPTOR["id"] == "hr_people" and DESCRIPTOR["version"] == 1
    cols = {c["key"]: c for c in DESCRIPTOR["columns"]}
    assert list(cols) == [
        "mois", "salarie", "service", "heures_theoriques", "heures_absence", "masse_salariale", "entree", "sortie",
        "motif_absence",
    ]
    assert all(set(c) == {"key", "label", "type", "required", "default"} for c in cols.values())
    assert {k for k, c in cols.items() if c["required"]} == {
        "mois", "salarie", "heures_theoriques", "heures_absence", "masse_salariale",
    }
    assert cols["mois"]["type"] == "date" and cols["entree"]["type"] == "date"
    assert cols["salarie"]["type"] == "string" and cols["masse_salariale"]["type"] == "number"
    assert all(c["type"] in ("date", "string", "number") for c in cols.values())
    assert all(c["default"] == c["default"].lower() for c in cols.values())
    params = {p["key"]: p for p in DESCRIPTOR["params"]}
    assert params and all(set(p) == {"key", "label", "type", "default", "min", "max"} for p in params.values())
    assert all(p["type"] == "integer" and p["min"] <= p["default"] <= p["max"] for p in params.values())


def test_spec_valide_et_sans_chevauchement():
    for kwargs in ({}, FULL, {"department": "service"}, {"exit_date": "sortie"}):
        spec = hr_people("ds-1", **kwargs)
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
    spec = hr_people("ds-1", **FULL)
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
        refs |= {c["key"] for c in v.get("columns", [])} - {"service"}
        if v["type"] == "text":
            refs |= {r for r in tmpl.findall(v["text"]) if r not in ("filters", "period")}
        assert refs <= measures | fields, (v.get("title"), refs - measures - fields)


def test_seuils_par_defaut_et_parametres():
    spec = hr_people("ds-1", **FULL)
    m = {x["name"]: x for x in spec["sources"][0]["measures"]}
    assert m["absenteisme"]["thresholds"] == {"direction": "lower_is_better", "good": 0.05, "warn": 0.08}
    assert m["turnover"]["thresholds"] == {"direction": "lower_is_better", "good": 0.10, "warn": 0.20}
    via_build = build("ds-1", {c["key"]: c["default"] for c in DESCRIPTOR["columns"]},
                      {"absenteeism_warn": 3, "absenteeism_bad": 6, "turnover_warn": 8, "turnover_bad": 15})
    m2 = {x["name"]: x for x in via_build["sources"][0]["measures"]}
    assert m2["absenteisme"]["thresholds"]["good"] == 0.03 and m2["absenteisme"]["thresholds"]["warn"] == 0.06
    assert m2["turnover"]["thresholds"]["good"] == 0.08 and m2["turnover"]["thresholds"]["warn"] == 0.15
    bar = next(c["visual"] for c in via_build["components"] if c["visual"].get("title") == "Absentéisme par mois")
    assert [r["value"] for r in bar["reference_lines"]] == [0.03, 0.06]


def test_seuils_incoherents():
    with pytest.raises(ValueError, match="absenteeism_warn"):
        hr_people("ds-1", absenteeism_warn=9, absenteeism_bad=8)
    with pytest.raises(ValueError, match="turnover_warn"):
        hr_people("ds-1", turnover_warn=20, turnover_bad=20)


def test_composants_conditionnels():
    titles = lambda s: {c["visual"].get("title") for c in s["components"]}  # noqa: E731
    base = titles(hr_people("ds-1"))
    assert "Turnover" not in base
    assert {"Masse salariale par service", "Entrées et sorties par mois", "Motifs d'absence",
            "Indicateurs par service"}.isdisjoint(base)
    assert {"Taux d'absentéisme", "Effectif", "Masse salariale", "Coût moyen par salarié",
            "Absentéisme par mois"} <= base
    full = titles(hr_people("ds-1", **FULL))
    assert {"Turnover", "Masse salariale par service", "Entrées et sorties par mois", "Motifs d'absence",
            "Indicateurs par service"} <= full
    assert {f["id"] for f in hr_people("ds-1")["filters"]} == {"periode"}
    assert {f["id"] for f in hr_people("ds-1", department="service")["filters"]} == {"periode", "service"}
    only_exit = next(c["visual"] for c in hr_people("ds-1", exit_date="sortie")["components"]
                     if c["visual"].get("title") == "Entrées et sorties par mois")
    assert [s["key"] for s in only_exit["series"]] == ["sorties"]


@pytest.mark.parametrize(
    "kwargs",
    [{"month": "Mois"}, {"employee": "a b"}, {"payroll": "1x"}, {"department": "Service"}, {"exit_date": "sortie-le"}],
)
def test_noms_de_colonnes_invalides(kwargs):
    with pytest.raises(ValueError, match="not a valid column name"):
        hr_people("ds-1", **kwargs)


def test_build_appelle_le_gabarit_par_mots_cles():
    columns = {c["key"]: c["default"] for c in DESCRIPTOR["columns"]}
    assert build("ds-1", columns, {}) == hr_people("ds-1", **FULL)
    columns.update(service="", sortie=None, salarie="employe")
    assert build("ds-1", columns, {}) == hr_people("ds-1", employee="employe", hire_date="entree",
                                                   absence_reason="motif_absence")


@pytest.mark.parametrize("missing", ["mois", "salarie", "heures_theoriques", "heures_absence", "masse_salariale"])
def test_build_colonne_obligatoire_manquante(missing):
    columns = {c["key"]: c["default"] for c in DESCRIPTOR["columns"]}
    columns[missing] = None
    with pytest.raises(ValueError, match="not a valid column name"):
        build("ds-1", columns, {})


# ---------------------------------------------------------------------------
# Intégration sans mock du moteur, valeurs calculées à la main
# ---------------------------------------------------------------------------
# Fenêtre mars 2025 - février 2026 (12 mois) :
#   heures théoriques 1800 + 1800 + 1050 (C, 7 mois) + 600 (D, 4 mois) = 5250
#   heures d'absence 72 + 180 + 52,5 + 30 = 334,5       -> absentéisme 334,5 / 5250
#   salariés-mois 12 + 12 + 7 + 4 = 35 sur 12 mois     -> effectif moyen 35 / 12
#   1 départ (C, sept. 2025)                             -> turnover 1 / (35 / 12) = 12 / 35
#   masse salariale 36000 + 48000 + 17500 + 8000 = 109500 ; coût moyen 109500 / 35
#   période précédente (janv.-fév. 2025) : 3 x 2 mois, masse 19000
ABSENTEISME = 334.5 / 5250
EFFECTIF = 35 / 12
TURNOVER = 12 / 35
MASSE = 109_500
COUT_MOYEN = 109_500 / 35


def test_integration_kpi_calcules_a_la_main(monkeypatch):
    dash, resp = _compute(monkeypatch, hr_people("ds-1", **FULL), _data())
    _no_errors(resp)
    kpis = _kpis(dash, resp)
    assert list(kpis) == KPI_TITLES
    assert kpis["Taux d'absentéisme"].value == pytest.approx(ABSENTEISME)
    assert kpis["Taux d'absentéisme"].status == "warn"  # 6,4 % : au-dessus de 5 %, sous 8 %
    assert kpis["Turnover"].value == pytest.approx(TURNOVER)
    assert kpis["Turnover"].status == "bad"
    assert kpis["Effectif"].value == pytest.approx(EFFECTIF)
    assert kpis["Masse salariale"].value == MASSE
    assert kpis["Coût moyen par salarié"].value == pytest.approx(COUT_MOYEN)
    # évolution face à la période précédente (19 000) : +476,3 %
    assert kpis["Masse salariale"].delta == pytest.approx((MASSE - 19_000) / 19_000)
    assert kpis["Masse salariale"].delta_formatted.startswith("+476")
    assert "1 départs" in kpis["Turnover"].secondary or "1 départ" in kpis["Turnover"].secondary
    assert kpis["Taux d'absentéisme"].spark is not None and len(kpis["Taux d'absentéisme"].spark) == 12


def test_la_fenetre_exclut_les_premiers_mois(monkeypatch):
    dash, resp = _compute(monkeypatch, hr_people("ds-1", **FULL), _data(), {"periode": "all"})
    _no_errors(resp)
    kpis = _kpis(dash, resp)
    # 14 mois : A et B 14 mois chacun, C 9 mois, D 4 mois -> 41 salariés-mois
    assert kpis["Masse salariale"].value == 14 * 3000 + 14 * 4000 + 9 * 2500 + 4 * 2000
    assert kpis["Effectif"].value == pytest.approx(41 / 14)


def test_graphiques(monkeypatch):
    dash, resp = _compute(monkeypatch, hr_people("ds-1", **FULL), _data())
    _no_errors(resp)
    _, absence = _component(dash, resp, "Absentéisme par mois")
    assert len(absence.rows) == 12
    assert absence.rows[0]["x"].startswith("2025-03") and absence.rows[-1]["x"].startswith("2026-02")
    by_month = {r["x"][:7]: r for r in absence.rows}
    assert by_month["2025-09"]["absenteisme"] == pytest.approx(28.5 / 450)  # A + B + C
    assert by_month["2025-12"]["absenteisme"] == pytest.approx(41 / 450)  # A + B + D
    assert by_month["2025-12"]["_color_absenteisme"] == "bad"  # 9,1 % > 8 %
    assert by_month["2025-03"]["_color_absenteisme"] == "warn"  # 28,5 / 450 = 6,3 %
    assert by_month["2026-01"]["_color_absenteisme"] == "good"  # 21 / 450 = 4,7 %
    assert [r["value"] for r in absence.reference_lines] == [0.05, 0.08]

    _, flows = _component(dash, resp, "Entrées et sorties par mois")
    by_flow = {r["x"][:7]: r for r in flows.rows}
    assert sum(r["entrees"] for r in flows.rows) == 1 and by_flow["2025-11"]["entrees"] == 1
    assert sum(r["sorties"] for r in flows.rows) == 1 and by_flow["2025-09"]["sorties"] == 1

    _, payroll = _component(dash, resp, "Masse salariale et effectif")
    by_pay = {r["x"][:7]: r for r in payroll.rows}
    assert by_pay["2025-09"]["masse_totale"] == 3000 + 4000 + 2500 and by_pay["2025-09"]["effectif"] == 3
    assert by_pay["2025-10"]["masse_totale"] == 7000 and by_pay["2025-10"]["effectif"] == 2
    assert by_pay["2025-11"]["masse_totale"] == 3000 + 4000 + 2000

    _, par_service = _component(dash, resp, "Masse salariale par service")
    assert {r["x"]: r["masse_totale"] for r in par_service.rows} == {"Compta": 84_000, "Prod": 25_500}
    assert par_service.rows[0]["x"] == "Compta"  # le plus grand d'abord

    _, motifs = _component(dash, resp, "Motifs d'absence")
    assert {r["x"]: r["heures_absence_total"] for r in motifs.rows} == {
        "Maladie": 72 + 180, "Congé sans solde": 52.5, "Accident du travail": 10, "Non précisé": 20,
    }


def test_tableau_par_service_et_ligne_toutes(monkeypatch):
    dash, resp = _compute(monkeypatch, hr_people("ds-1", **FULL), _data())
    kpis = _kpis(dash, resp)
    _, table = _component(dash, resp, "Indicateurs par service")
    rows = {r["service"]: r for r in table.rows if not r.get("_total")}
    assert rows["Compta"]["masse_totale"] == 84_000 and rows["Compta"]["effectif"] == 2
    assert rows["Compta"]["absenteisme"] == pytest.approx(252 / 3600)
    assert rows["Compta"]["turnover"] == 0
    assert rows["Prod"]["masse_totale"] == 25_500
    assert rows["Prod"]["absenteisme"] == pytest.approx(82.5 / 1650)
    total = next(r for r in table.rows if r.get("_total"))
    assert total["absenteisme"] == pytest.approx(kpis["Taux d'absentéisme"].value)
    assert total["turnover"] == pytest.approx(kpis["Turnover"].value)
    assert total["masse_totale"] == kpis["Masse salariale"].value
    assert total["effectif"] == pytest.approx(kpis["Effectif"].value)


def test_filtre_service(monkeypatch):
    dash, resp = _compute(monkeypatch, hr_people("ds-1", **FULL), _data(), {"service": "Prod"})
    _no_errors(resp)
    kpis = _kpis(dash, resp)
    assert kpis["Masse salariale"].value == 25_500
    assert kpis["Taux d'absentéisme"].value == pytest.approx(82.5 / 1650)
    assert kpis["Taux d'absentéisme"].status == "good"  # 5,0 % : à la limite incluse
    assert kpis["Turnover"].value > 0


def test_seuils_personnalises_changent_le_statut(monkeypatch):
    spec = hr_people("ds-1", **FULL, absenteeism_warn=7, absenteeism_bad=9, turnover_warn=40, turnover_bad=50)
    dash, resp = _compute(monkeypatch, spec, _data())
    kpis = _kpis(dash, resp)
    assert kpis["Taux d'absentéisme"].status == "good"  # 6,4 % <= 7 %
    assert kpis["Turnover"].status == "good"  # 34,3 % <= 40 %


def test_mois_en_fin_de_mois_et_colonnes_renommees(monkeypatch):
    df = _data()
    df["mois"] = df["mois"] + pd.offsets.MonthEnd(0)
    df = df.rename(columns={
        "mois": "periode", "salarie": "nom", "service": "equipe", "heures_theoriques": "h_theo",
        "heures_absence": "h_abs", "masse_salariale": "cout", "entree": "date_in", "sortie": "date_out",
        "motif_absence": "raison",
    })
    spec = hr_people(
        "ds-1", month="periode", employee="nom", department="equipe", theoretical_hours="h_theo",
        absence_hours="h_abs", payroll="cout", hire_date="date_in", exit_date="date_out", absence_reason="raison",
    )
    assert validate_spec(_dashboard(spec)) == []
    dash, resp = _compute(monkeypatch, spec, df)
    _no_errors(resp)
    kpis = _kpis(dash, resp)
    assert kpis["Taux d'absentéisme"].value == pytest.approx(ABSENTEISME)
    assert kpis["Turnover"].value == pytest.approx(TURNOVER)
    assert kpis["Masse salariale"].value == MASSE


def test_sans_colonnes_optionnelles(monkeypatch):
    df = _data().drop(columns=["service", "entree", "sortie", "motif_absence"])
    spec = hr_people("ds-1")
    dash, resp = _compute(monkeypatch, spec, df)
    _no_errors(resp)
    kpis = _kpis(dash, resp)
    assert list(kpis) == ["Taux d'absentéisme", "Effectif", "Masse salariale", "Coût moyen par salarié"]
    assert kpis["Taux d'absentéisme"].value == pytest.approx(ABSENTEISME)
    assert kpis["Masse salariale"].value == MASSE
    titles = {c.visual.title for c in dash.components}
    assert not titles & {"Masse salariale par service", "Entrées et sorties par mois", "Motifs d'absence",
                         "Indicateurs par service"}


def test_seulement_la_sortie_et_le_service(monkeypatch):
    df = _data().drop(columns=["entree", "motif_absence"])
    dash, resp = _compute(monkeypatch, hr_people("ds-1", department="service", exit_date="sortie"), df)
    _no_errors(resp)
    assert _kpis(dash, resp)["Turnover"].value == pytest.approx(TURNOVER)


def test_salarie_sans_heures_ni_absence_ne_casse_pas(monkeypatch):
    df = _data()
    df["heures_absence"] = 0
    dash, resp = _compute(monkeypatch, hr_people("ds-1", **FULL), df)
    _no_errors(resp)
    kpis = _kpis(dash, resp)
    assert kpis["Taux d'absentéisme"].value == 0 and kpis["Taux d'absentéisme"].status == "good"
    _, motifs = _component(dash, resp, "Motifs d'absence")
    assert motifs.rows == []

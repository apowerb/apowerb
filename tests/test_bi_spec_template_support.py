"""Gabarit « Support et SLA » : spec valide, grille sans chevauchement, mesures existantes,
paramètres d'objectif, moteur réel sur un jeu synthétique (colonnes par défaut, renommées, sans optionnelles)."""
import random

import pandas as pd
import pytest

from apowerb.bi.spec.catalog import get_template
from apowerb.bi.spec.templates_support import DESCRIPTOR, build, support_sla
from apowerb.bi.spec.validate import validate_spec
from tests.helpers.bi_template_engine import (
    assert_grid_ok,
    assert_references_exist,
    compute,
    component,
    errors,
    kpis,
    titles,
)
from tests.helpers.bi_template_env import dashboard_from_spec

KPI_TITLES = ["Tickets reçus", "Backlog ouvert", "Délai médian de première réponse", "SLA respecté"]
FULL = dict(
    first_response_at="premiere_reponse", resolved_at="resolu_le", priority="priorite", team="equipe",
)


def _synthetic_tickets() -> pd.DataFrame:
    rnd = random.Random(21)
    cutoff = pd.Timestamp("2025-10-31 18:00")
    rows = []
    for i in range(900):
        opened = pd.Timestamp("2025-04-01 08:00") + pd.Timedelta(minutes=rnd.randrange(0, 214 * 24 * 60))
        answered = opened + pd.Timedelta(hours=rnd.choice([0.5, 1, 2, 3, 5, 8, 30]))
        solved = answered + pd.Timedelta(hours=rnd.choice([2, 6, 20, 40, 60, 100, 200]))
        if rnd.random() < 0.06:
            solved = pd.NaT
        if solved is not pd.NaT and solved > cutoff:
            solved = pd.NaT
        if answered > cutoff or (solved is pd.NaT and rnd.random() < 0.4):
            answered = pd.NaT
        rows.append({
            "ticket_id": f"T{i:05d}",
            "date_ouverture": opened,
            "premiere_reponse": answered,
            "resolu_le": solved,
            "priorite": rnd.choice(["Haute", "Moyenne", "Moyenne", "Basse"]),
            "equipe": rnd.choice(["Niveau 1", "Niveau 2", "Facturation"]),
        })
    return pd.DataFrame(rows)


def _window(df: pd.DataFrame, weeks: int | None = 26) -> pd.DataFrame:
    if weeks is None:
        return df
    return df[df["date_ouverture"] > df["date_ouverture"].max() - pd.Timedelta(weeks=weeks)]


def _flags(df: pd.DataFrame, response_h: int, resolution_h: int) -> pd.DataFrame:
    as_of = max(df[c].max() for c in ("date_ouverture", "premiere_reponse", "resolu_le"))
    hours = lambda end: (end - df["date_ouverture"]).dt.total_seconds() / 3600  # noqa: E731
    first = hours(df["premiere_reponse"])
    solve = hours(df["resolu_le"])
    since = (as_of - df["date_ouverture"]).dt.total_seconds() / 3600
    breach_first = (first > response_h) | (df["premiere_reponse"].isna() & (since > response_h))
    breach_solve = (solve > resolution_h) | (df["resolu_le"].isna() & (since > resolution_h))
    return df.assign(first_h=first, solve_h=solve, sla_ok=~(breach_first | breach_solve))


def _expected(df: pd.DataFrame, response_h: int = 4, resolution_h: int = 48, weeks: int | None = 26) -> dict:
    w = _window(_flags(df, response_h, resolution_h), weeks)
    return {
        "tickets": float(w["ticket_id"].nunique()),
        "backlog": float(w["resolu_le"].isna().sum()),
        "median_first": float(w["first_h"].median()),
        "sla": float(w["sla_ok"].mean()),
        "frame": w,
    }


@pytest.fixture(params=[{}, FULL], ids=["sans_optionnelles", "complet"])
def spec(request):
    return support_sla("ds-1", **request.param)


def test_spec_valide(spec):
    assert validate_spec(dashboard_from_spec(spec)) == []


def test_pas_de_chevauchement_et_dans_la_grille(spec):
    assert_grid_ok(spec)


def test_chaque_mesure_referencee_existe(spec):
    assert_references_exist(spec, {"priorite", "equipe", "ticket_id"})


def test_composants_conditionnels():
    base = titles(support_sla("ds-1"))
    assert "Tickets reçus" in base and "Tickets par semaine" in base
    assert not {"Backlog ouvert", "Délai médian de première réponse", "SLA respecté", "Tickets par priorité",
                "SLA par équipe"} & base
    assert "Backlog ouvert" in titles(support_sla("ds-1", resolved_at="resolu_le"))
    assert "Délai médian de première réponse" in titles(support_sla("ds-1", first_response_at="premiere_reponse"))
    full = titles(support_sla("ds-1", **FULL))
    assert {"Tickets par priorité", "SLA par équipe"} <= full


def test_seuils_suivent_les_objectifs():
    spec = support_sla("ds-1", first_response_at="premiere_reponse", response_target_hours=6)
    m = {x["name"]: x for x in spec["sources"][0]["measures"]}
    assert m["delai_reponse"]["thresholds"] == {"direction": "lower_is_better", "good": 6.0, "warn": 12.0}
    assert m["sla"]["thresholds"] == {"direction": "higher_is_better", "good": 0.95, "warn": 0.9}
    kpi = next(c["visual"] for c in spec["components"] if c["visual"].get("title") == "Délai médian de première réponse")
    assert "6 h" in kpi["kpi"]["target_label"]


@pytest.mark.parametrize(
    "kwargs",
    [{"response_target_hours": 0}, {"resolution_target_hours": -1}, {"response_target_hours": 2.5}],
)
def test_objectifs_invalides(kwargs):
    with pytest.raises(ValueError, match="objectif"):
        support_sla("ds-1", **kwargs)


def test_noms_de_colonnes_invalides():
    for kwargs in ({"ticket": "a b"}, {"opened_at": "Date"}, {"team": "x-y"}, {"priority": "1x"}):
        with pytest.raises(ValueError, match="not a valid column name"):
            support_sla("ds-1", **kwargs)


def test_descripteur():
    assert DESCRIPTOR["id"] == "support_sla" and DESCRIPTOR["version"] == 1
    assert DESCRIPTOR["label"]["fr"] == "Support et SLA"
    assert {"id", "label", "description", "version", "columns", "params"} == set(DESCRIPTOR)
    cols = {c["key"]: c for c in DESCRIPTOR["columns"]}
    assert set(cols) == {"ticket", "opened_at", "first_response_at", "resolved_at", "priority", "team"}
    assert all(set(c) == {"key", "label", "type", "required", "default"} for c in cols.values())
    assert {k for k, c in cols.items() if c["required"]} == {"ticket", "opened_at"}
    assert cols["opened_at"]["type"] == "date" and cols["team"]["type"] == "string"
    params = {p["key"]: p for p in DESCRIPTOR["params"]}
    assert set(params) == {"response_target_hours", "resolution_target_hours"}
    assert all(p["type"] == "integer" and p["min"] < p["default"] < p["max"] for p in params.values())


def test_gabarit_du_catalogue_valide_ses_parametres():
    template = get_template("support_sla")
    assert template is not None
    _, errs = template.clean_params({"response_target_hours": 0})
    assert errs and "compris entre" in errs[0]
    clean, errs = template.clean_params({})
    assert errs == [] and clean == {"response_target_hours": 4, "resolution_target_hours": 48}


def test_build_appelle_le_gabarit_par_mots_cles():
    columns = {c["key"]: c["default"] for c in DESCRIPTOR["columns"]}
    params = {"response_target_hours": 2, "resolution_target_hours": 24}
    assert build("ds-1", columns, params) == support_sla("ds-1", response_target_hours=2, resolution_target_hours=24)
    columns.update(team="equipe", ticket="numero")
    assert build("ds-1", columns, params) == support_sla(
        "ds-1", team="equipe", ticket="numero", response_target_hours=2, resolution_target_hours=24
    )
    columns["team"] = ""
    assert build("ds-1", columns, {}) == support_sla("ds-1", ticket="numero")


def test_build_colonne_requise_vide():
    columns = {c["key"]: c["default"] for c in DESCRIPTOR["columns"]}
    columns["opened_at"] = None
    with pytest.raises(ValueError, match="not a valid column name"):
        build("ds-1", columns, {})


# ---------------------------------------------------------------------------
# Intégration sans mock du moteur
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("targets", [(4, 48), (2, 24)], ids=["objectifs_par_defaut", "objectifs_serres"])
def test_integration_moteur_sans_mock(monkeypatch, targets):
    df = _synthetic_tickets()
    spec = support_sla("ds-1", response_target_hours=targets[0], resolution_target_hours=targets[1], **FULL)
    dash, resp = compute(monkeypatch, spec, df)
    assert errors(resp) == {}
    k = kpis(dash, resp)
    assert list(k) == KPI_TITLES
    exp = _expected(df, *targets)
    assert k["Tickets reçus"].value == exp["tickets"]
    assert k["Backlog ouvert"].value == exp["backlog"] > 0
    assert k["Délai médian de première réponse"].value == pytest.approx(exp["median_first"])
    assert k["SLA respecté"].value == pytest.approx(exp["sla"])
    assert 0.1 < exp["sla"] < 0.95 and k["SLA respecté"].status in ("warn", "bad")
    assert k["Tickets reçus"].spark is not None and len(k["Tickets reçus"].spark) >= 2


def test_objectif_plus_serre_baisse_le_sla(monkeypatch):
    df = _synthetic_tickets()
    values = []
    for hours in (24, 2):
        dash, resp = compute(monkeypatch, support_sla("ds-1", resolved_at="resolu_le", first_response_at="premiere_reponse", response_target_hours=hours), df)
        values.append(kpis(dash, resp)["SLA respecté"].value)
    assert values[1] < values[0]


def test_graphiques_et_tableau(monkeypatch):
    df = _synthetic_tickets()
    dash, resp = compute(monkeypatch, support_sla("ds-1", **FULL), df)
    exp = _expected(df)
    w = exp["frame"]
    _, weekly = component(dash, resp, "Tickets par semaine")
    assert sum(r["tickets"] for r in weekly.rows) == exp["tickets"]
    assert len(weekly.rows) >= 26
    assert all(pd.Timestamp(r["x"]).dayofweek == 0 for r in weekly.rows)
    _, by_priority = component(dash, resp, "Tickets par priorité")
    assert {r["priorite"]: r["tickets"] for r in by_priority.rows} == w.groupby("priorite")["ticket_id"].nunique().to_dict()
    _, table = component(dash, resp, "SLA par équipe")
    total = next(r for r in table.rows if r.get("_total"))
    teams = [r for r in table.rows if not r.get("_total")]
    assert {r["equipe"] for r in teams} == {"Niveau 1", "Niveau 2", "Facturation"}
    by_team = w.groupby("equipe")
    for row in teams:
        group = by_team.get_group(row["equipe"])
        assert row["tickets"] == group["ticket_id"].nunique()
        assert row["backlog"] == group["resolu_le"].isna().sum()
        assert row["delai_reponse"] == pytest.approx(group["first_h"].median())
        assert row["sla"] == pytest.approx(group["sla_ok"].mean())
    assert total["tickets"] == exp["tickets"] and total["sla"] == pytest.approx(exp["sla"])


def test_filtre_equipe(monkeypatch):
    df = _synthetic_tickets()
    dash, resp = compute(monkeypatch, support_sla("ds-1", **FULL), df, {"equipe": "Niveau 2"})
    assert errors(resp) == {}
    sub = _expected(df)["frame"]
    sub = sub[sub["equipe"] == "Niveau 2"]
    k = kpis(dash, resp)
    assert k["Tickets reçus"].value == sub["ticket_id"].nunique()
    assert k["SLA respecté"].value == pytest.approx(sub["sla_ok"].mean())


def test_colonnes_renommees(monkeypatch):
    df = _synthetic_tickets().rename(columns={
        "ticket_id": "numero", "date_ouverture": "cree_le", "premiere_reponse": "repondu_le", "resolu_le": "clos_le",
        "priorite": "urgence", "equipe": "groupe",
    })
    spec = support_sla(
        "ds-1", ticket="numero", opened_at="cree_le", first_response_at="repondu_le", resolved_at="clos_le",
        priority="urgence", team="groupe",
    )
    assert validate_spec(dashboard_from_spec(spec)) == []
    dash, resp = compute(monkeypatch, spec, df)
    assert errors(resp) == {}
    k = kpis(dash, resp)
    assert list(k) == KPI_TITLES
    assert k["SLA respecté"].value == pytest.approx(_expected(_synthetic_tickets())["sla"])


def test_sans_colonnes_optionnelles(monkeypatch):
    df = _synthetic_tickets()[["ticket_id", "date_ouverture"]]
    dash, resp = compute(monkeypatch, support_sla("ds-1"), df)
    assert errors(resp) == {}
    k = kpis(dash, resp)
    assert list(k) == ["Tickets reçus"] and k["Tickets reçus"].value == _expected(_synthetic_tickets())["tickets"]


def test_sans_date_de_resolution_pas_de_backlog(monkeypatch):
    df = _synthetic_tickets()
    dash, resp = compute(monkeypatch, support_sla("ds-1", first_response_at="premiere_reponse"), df)
    assert errors(resp) == {}
    k = kpis(dash, resp)
    assert list(k) == ["Tickets reçus", "Délai médian de première réponse", "SLA respecté"]
    w = _window(_flags(df, 4, 48))
    as_of = df[["date_ouverture", "premiere_reponse"]].max().max()
    since = (as_of - w["date_ouverture"]).dt.total_seconds() / 3600
    breach = (w["first_h"] > 4) | (w["premiere_reponse"].isna() & (since > 4))
    assert k["SLA respecté"].value == pytest.approx(float((~breach).mean()))


def test_aucun_ticket_resolu_ne_casse_pas(monkeypatch):
    df = _synthetic_tickets()
    df["resolu_le"] = pd.NaT
    dash, resp = compute(monkeypatch, support_sla("ds-1", **FULL), df)
    assert errors(resp) == {}
    assert kpis(dash, resp)["Backlog ouvert"].value == kpis(dash, resp)["Tickets reçus"].value

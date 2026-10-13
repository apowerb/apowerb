"""Gabarit « Balance âgée clients » : spec valide, grille sans chevauchement, mesures
existantes, moteur réel sur un jeu synthétique (colonnes par défaut, renommées, sans optionnelles)."""
import random
import re

import pandas as pd
import pytest

from apowerb.bi.spec.templates_receivables import DESCRIPTOR, build, receivables_aging
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

BASE_KPIS = ["Encours total", "Encours échu", "% échu", "DSO"]
BUCKETS = ["1. Non échu", "2. 0-30 j", "3. 31-60 j", "4. 61-90 j", "5. > 90 j"]
FULL = dict(invoice="facture", open_amount="reste_du", paid_amount="regle", payment_date="date_paiement")


def _synthetic_invoices() -> pd.DataFrame:
    rnd = random.Random(7)
    as_of = pd.Timestamp("2025-10-31")
    rows = []
    for i in range(700):
        invoiced = pd.Timestamp("2024-11-01") + pd.Timedelta(days=rnd.randrange(0, 365))
        due = invoiced + pd.Timedelta(days=rnd.choice([30, 45, 60]))
        amount = float(rnd.randrange(500, 20000))
        roll = rnd.random()
        paid, paid_on = 0.0, pd.NaT
        if roll < 0.65:
            paid_on = due + pd.Timedelta(days=rnd.randrange(-10, 45))
            paid = amount
            if paid_on > as_of:
                paid_on, paid = pd.NaT, 0.0
        elif roll < 0.8:
            paid = round(amount * 0.4)
        rows.append({
            "client": f"Client {rnd.randrange(12):02d}",
            "facture": f"F{i:05d}",
            "date_facture": invoiced,
            "date_echeance": due,
            "montant": amount,
            "regle": paid,
            "reste_du": amount - paid,
            "date_paiement": paid_on,
        })
    df = pd.DataFrame(rows)
    df.loc[0, "date_facture"] = as_of
    df.loc[0, "date_echeance"] = as_of + pd.Timedelta(days=30)
    return df


def _expected(df: pd.DataFrame, reste: pd.Series) -> dict:
    as_of = df["date_facture"].max()
    late_days = (as_of - df["date_echeance"]).dt.days
    open_ = reste > 0
    encours = float(reste.sum())
    echu = float(reste[late_days > 0].sum())
    ca90 = float(df.loc[(as_of - df["date_facture"]).dt.days < 90, "montant"].sum())
    bucket = pd.cut(late_days[open_], [-10_000, 0, 30, 60, 90, 10_000], labels=BUCKETS)
    return {
        "encours": encours,
        "echu": echu,
        "pct_echu": echu / encours,
        "dso": encours / ca90 * 90,
        "ca": float(df["montant"].sum()),
        "buckets": reste[open_].groupby(bucket, observed=False).sum().to_dict(),
        "late_days": late_days,
    }


VARIANTS = {
    "aucune": ({}, lambda d: d["montant"]),
    "date_paiement": ({"payment_date": "date_paiement"}, lambda d: d["montant"].where(d["date_paiement"].isna(), 0)),
    "montant_regle": ({"paid_amount": "regle"}, lambda d: d["montant"] - d["regle"]),
    "reste_du": ({"open_amount": "reste_du"}, lambda d: d["reste_du"]),
    "reste_du_prioritaire": (
        {"open_amount": "reste_du", "payment_date": "date_paiement", "paid_amount": "regle"},
        lambda d: d["reste_du"],
    ),
}


@pytest.fixture(params=[{}, FULL], ids=["sans_optionnelles", "complet"])
def spec(request):
    return receivables_aging("ds-1", **request.param)


def test_spec_valide(spec):
    assert validate_spec(dashboard_from_spec(spec)) == []


def test_pas_de_chevauchement_et_dans_la_grille(spec):
    assert_grid_ok(spec)


def test_chaque_mesure_referencee_existe(spec):
    assert_references_exist(spec, {"client", "facture"})


def test_seuils_et_formats():
    m = {x["name"]: x for x in receivables_aging("ds-1")["sources"][0]["measures"]}
    assert m["pct_echu"]["thresholds"]["direction"] == "lower_is_better" and m["pct_echu"]["format"] == "percent"
    assert m["dso"]["thresholds"] == {"direction": "lower_is_better", "good": 45.0, "warn": 60.0}
    assert m["dso"]["unit"] == "jours" and "90" in m["dso"]["description"]


def test_composants_conditionnels():
    base = titles(receivables_aging("ds-1"))
    assert "Délai de règlement moyen" not in base and "Plus grosses factures échues" not in base
    full = titles(receivables_aging("ds-1", **FULL))
    assert {"Délai de règlement moyen", "Plus grosses factures échues"} <= full


def test_description_precise_la_date_de_reference():
    text = DESCRIPTOR["description"]["fr"].lower()
    assert "dernière date de facture" in text and "dso" in text


def test_noms_de_colonnes_invalides():
    for kwargs in ({"customer": "a b"}, {"amount": "1x"}, {"due_date": "Date"}, {"invoice": "x-y"}):
        with pytest.raises(ValueError, match="not a valid column name"):
            receivables_aging("ds-1", **kwargs)


def test_descripteur():
    assert DESCRIPTOR["id"] == "receivables_aging" and DESCRIPTOR["version"] == 1 and DESCRIPTOR["params"] == []
    assert DESCRIPTOR["label"]["fr"] == "Balance âgée clients"
    assert {"id", "label", "description", "version", "columns", "params"} == set(DESCRIPTOR)
    cols = {c["key"]: c for c in DESCRIPTOR["columns"]}
    assert set(cols) == {
        "customer", "invoice", "invoice_date", "due_date", "amount", "open_amount", "paid_amount", "payment_date",
    }
    assert all(set(c) == {"key", "label", "type", "required", "default"} for c in cols.values())
    assert {k for k, c in cols.items() if c["required"]} == {"customer", "invoice_date", "due_date", "amount"}
    assert cols["amount"]["type"] == "number" and cols["due_date"]["type"] == "date"
    assert cols["invoice"]["type"] == "string" and cols["invoice"]["default"] is None


def test_build_appelle_le_gabarit_par_mots_cles():
    columns = {c["key"]: c["default"] for c in DESCRIPTOR["columns"]}
    assert build("ds-1", columns, {}) == receivables_aging("ds-1")
    columns.update(invoice="facture", customer="acheteur")
    assert build("ds-1", columns, {}) == receivables_aging("ds-1", invoice="facture", customer="acheteur")
    columns["invoice"] = ""
    assert build("ds-1", columns, {}) == receivables_aging("ds-1", customer="acheteur")


def test_build_colonne_requise_vide():
    columns = {c["key"]: c["default"] for c in DESCRIPTOR["columns"]}
    columns["amount"] = None
    with pytest.raises(ValueError, match="not a valid column name"):
        build("ds-1", columns, {})


# ---------------------------------------------------------------------------
# Intégration sans mock du moteur
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("variant", list(VARIANTS))
def test_integration_moteur_sans_mock(monkeypatch, variant):
    kwargs, reste_of = VARIANTS[variant]
    df = _synthetic_invoices()
    dash, resp = compute(monkeypatch, receivables_aging("ds-1", **kwargs), df)
    assert errors(resp) == {}
    k = kpis(dash, resp)
    expected_titles = BASE_KPIS + (["Délai de règlement moyen"] if "payment_date" in kwargs else [])
    assert list(k) == expected_titles
    exp = _expected(df, reste_of(df))
    assert k["Encours total"].value == pytest.approx(exp["encours"])
    assert k["Encours échu"].value == pytest.approx(exp["echu"])
    assert k["% échu"].value == pytest.approx(exp["pct_echu"])
    assert k["DSO"].value == pytest.approx(exp["dso"])
    assert k["DSO"].status in ("good", "warn", "bad")
    assert k["Encours total"].spark is not None and len(k["Encours total"].spark) >= 2
    if "payment_date" in kwargs:
        paid = df[df["date_paiement"].notna()]
        assert k["Délai de règlement moyen"].value == pytest.approx((paid["date_paiement"] - paid["date_facture"]).dt.days.mean())


def test_tranches_d_anciennete(monkeypatch):
    df = _synthetic_invoices()
    kwargs, reste_of = VARIANTS["reste_du"]
    dash, resp = compute(monkeypatch, receivables_aging("ds-1", **kwargs), df)
    exp = _expected(df, reste_of(df))
    _, bar = component(dash, resp, "Encours par ancienneté")
    assert [r["x"] for r in bar.rows] == BUCKETS
    for row in bar.rows:
        assert row["encours"] == pytest.approx(exp["buckets"][row["x"]])
    assert sum(r["encours"] for r in bar.rows) == pytest.approx(exp["encours"])
    assert sum(r["encours"] for r in bar.rows[1:]) == pytest.approx(exp["echu"])


def test_top_clients_en_retard(monkeypatch):
    df = _synthetic_invoices()
    kwargs, reste_of = VARIANTS["reste_du"]
    dash, resp = compute(monkeypatch, receivables_aging("ds-1", **kwargs), df)
    reste = reste_of(df)
    late = df["date_facture"].max() > df["date_echeance"]
    by_client = reste[late].groupby(df["client"]).sum().sort_values(ascending=False).head(10)
    _, chart = component(dash, resp, "Clients les plus en retard")
    assert [r["client"] for r in chart.rows] == list(by_client.index)
    assert [r["encours_echu"] for r in chart.rows] == pytest.approx(list(by_client.values))


def test_facturation_mensuelle(monkeypatch):
    df = _synthetic_invoices()
    dash, resp = compute(monkeypatch, receivables_aging("ds-1"), df)
    _, combo = component(dash, resp, "Facturation mensuelle")
    assert len(combo.rows) >= 12
    assert sum(r["ca_facture"] for r in combo.rows) == pytest.approx(df["montant"].sum())
    assert [r["x"] for r in combo.rows] == sorted(r["x"] for r in combo.rows)


def test_tableau_clients_et_ligne_toutes(monkeypatch):
    df = _synthetic_invoices()
    dash, resp = compute(monkeypatch, receivables_aging("ds-1", payment_date="date_paiement"), df)
    k = kpis(dash, resp)
    _, table = component(dash, resp, "Encours par client")
    total = next(r for r in table.rows if r.get("_total"))
    clients = [r for r in table.rows if not r.get("_total")]
    assert len(clients) == 12 and total["client"] == "Toutes"
    assert total["encours"] == pytest.approx(k["Encours total"].value)
    assert total["encours_echu"] == pytest.approx(k["Encours échu"].value)
    assert total["pct_echu"] == pytest.approx(k["% échu"].value)
    assert total["encours"] == pytest.approx(sum(r["encours"] for r in clients))


def test_factures_echues_les_plus_en_retard(monkeypatch):
    df = _synthetic_invoices()
    kwargs, reste_of = VARIANTS["reste_du"]
    dash, resp = compute(monkeypatch, receivables_aging("ds-1", invoice="facture", **kwargs), df)
    assert errors(resp) == {}
    reste = reste_of(df)
    late = (df["date_facture"].max() - df["date_echeance"]).dt.days
    top = df.assign(r=reste.where(late > 0)).sort_values("r", ascending=False, kind="stable").head(10)
    _, table = component(dash, resp, "Plus grosses factures échues")
    assert [r["facture"] for r in table.rows] == list(top["facture"])
    assert [r["encours_echu"] for r in table.rows] == pytest.approx(list(top["r"]))


def test_filtre_client(monkeypatch):
    df = _synthetic_invoices()
    kwargs, reste_of = VARIANTS["reste_du"]
    dash, resp = compute(monkeypatch, receivables_aging("ds-1", **kwargs), df, {"client": "Client 03"})
    assert errors(resp) == {}
    sub = df[df["client"] == "Client 03"]
    as_of = df["date_facture"].max()
    assert kpis(dash, resp)["Encours total"].value == pytest.approx(sub["reste_du"].sum())
    echu = sub["reste_du"][sub["date_echeance"] < as_of].sum()
    assert kpis(dash, resp)["Encours échu"].value == pytest.approx(echu)


def test_colonnes_renommees(monkeypatch):
    df = _synthetic_invoices().rename(columns={
        "client": "acheteur", "facture": "n_fact", "date_facture": "emis", "date_echeance": "echeance",
        "montant": "ttc", "reste_du": "solde", "regle": "paye", "date_paiement": "recu_le",
    })
    spec = receivables_aging(
        "ds-1", customer="acheteur", invoice="n_fact", invoice_date="emis", due_date="echeance", amount="ttc",
        open_amount="solde", paid_amount="paye", payment_date="recu_le",
    )
    assert validate_spec(dashboard_from_spec(spec)) == []
    dash, resp = compute(monkeypatch, spec, df)
    assert errors(resp) == {}
    k = kpis(dash, resp)
    ref = _synthetic_invoices()
    assert all(v.value is not None for v in k.values())
    assert k["Encours total"].value == pytest.approx(_expected(ref, ref["reste_du"])["encours"])


def test_aucune_facture_echue_ne_casse_pas(monkeypatch):
    df = _synthetic_invoices()
    df["date_echeance"] = df["date_facture"] + pd.Timedelta(days=400)
    dash, resp = compute(monkeypatch, receivables_aging("ds-1"), df)
    assert errors(resp) == {}
    k = kpis(dash, resp)
    assert k["Encours échu"].value == 0 and k["% échu"].value == 0


def test_tout_regle_ne_casse_pas(monkeypatch):
    df = _synthetic_invoices()
    df["date_paiement"] = df["date_echeance"]
    dash, resp = compute(monkeypatch, receivables_aging("ds-1", payment_date="date_paiement"), df)
    assert errors(resp) == {}
    k = kpis(dash, resp)
    assert k["Encours total"].value == 0 and k["% échu"].value is None


def test_periode_par_defaut_toute_la_periode():
    flt = next(f for f in receivables_aging("ds-1")["filters"] if f["id"] == "periode")
    assert flt["default"] == "all" and flt["field"] == "date_facture" and re.fullmatch(r"\d+[wm]", flt["presets"][0])

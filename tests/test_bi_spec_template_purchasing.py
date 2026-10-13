"""Gabarit « Achats et fournisseurs » : spec valide, grille sans chevauchement, mesures
existantes, moteur réel sur un jeu synthétique (colonnes par défaut, renommées, sans optionnelles)."""
import random

import pandas as pd
import pytest

from apowerb.bi.spec.templates_purchasing import DESCRIPTOR, build, purchasing_suppliers
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

BASE_KPIS = ["Dépenses totales", "Fournisseurs", "Part du top 5"]
DELIVERY_KPIS = ["Livré à l'heure", "Retard moyen"]
FULL = dict(quantity="quantite", promised_date="date_promise", received_date="date_reception")


def _synthetic_purchases() -> pd.DataFrame:
    rnd = random.Random(5)
    last = pd.Timestamp("2025-10-31")
    suppliers = [f"Fournisseur {i:02d}" for i in range(15)]
    weights = [30, 20, 12, 9, 7, 5, 4, 3, 3, 2, 2, 1, 1, 1, 1]
    items = [f"Article {i:02d}" for i in range(20)]
    rows = []
    for _ in range(1500):
        day = pd.Timestamp("2024-09-01") + pd.Timedelta(days=rnd.randrange(0, 426))
        promised = day + pd.Timedelta(days=rnd.randrange(5, 16))
        received = promised + pd.Timedelta(days=rnd.choice([-2, -1, 0, 0, 0, 1, 3, 6, 10]))
        if received > last:
            received = pd.NaT
        qty = float(rnd.randrange(1, 200))
        rows.append({
            "date": day,
            "fournisseur": rnd.choices(suppliers, weights)[0],
            "article": rnd.choice(items),
            "quantite": qty,
            "montant": round(qty * rnd.uniform(2, 80), 2),
            "date_promise": promised,
            "date_reception": received,
        })
    df = pd.DataFrame(rows)
    df.loc[0, "date"] = last
    return df


def _window(df: pd.DataFrame, months: int | None = 12) -> pd.DataFrame:
    if months is None:
        return df
    return df[df["date"] > df["date"].max() - pd.DateOffset(months=months)]


def _expected(df: pd.DataFrame, months: int | None = 12) -> dict:
    w = _window(df, months)
    by_supplier = w.groupby("fournisseur")["montant"].sum().sort_values(ascending=False)
    done = w[w["date_reception"].notna()]
    on_time = done["date_reception"] <= done["date_promise"]
    late = (done["date_reception"] - done["date_promise"]).dt.days[~on_time]
    return {
        "depenses": float(w["montant"].sum()),
        "fournisseurs": float(w["fournisseur"].nunique()),
        "top5": float(by_supplier.head(5).sum() / by_supplier.sum()),
        "a_l_heure": float(on_time.mean()),
        "retard_moyen": float(late.mean()),
        "by_supplier": by_supplier,
    }


@pytest.fixture(params=[{}, FULL], ids=["sans_optionnelles", "complet"])
def spec(request):
    return purchasing_suppliers("ds-1", **request.param)


def test_spec_valide(spec):
    assert validate_spec(dashboard_from_spec(spec)) == []


def test_pas_de_chevauchement_et_dans_la_grille(spec):
    assert_grid_ok(spec)


def test_chaque_mesure_referencee_existe(spec):
    assert_references_exist(spec, {"fournisseur", "article"})


def test_seuils_et_formats():
    m = {x["name"]: x for x in purchasing_suppliers("ds-1", **FULL)["sources"][0]["measures"]}
    assert m["part_top5"]["thresholds"] == {"direction": "lower_is_better", "good": 0.5, "warn": 0.7}
    assert m["a_l_heure"]["thresholds"] == {"direction": "higher_is_better", "good": 0.95, "warn": 0.9}
    assert m["a_l_heure"]["format"] == "percent" and m["part_top5"]["format"] == "percent"


def test_composants_conditionnels():
    base = titles(purchasing_suppliers("ds-1"))
    assert not {"Livré à l'heure", "Retard moyen"} & base
    assert "Détail par article" in base
    qty = purchasing_suppliers("ds-1", quantity="quantite")
    assert "prix_moyen" in {c["key"] for c in _table(qty, "Détail par article")["columns"]}
    assert "prix_moyen" not in {c["key"] for c in _table(purchasing_suppliers("ds-1"), "Détail par article")["columns"]}
    delivery = titles(purchasing_suppliers("ds-1", promised_date="date_promise", received_date="date_reception"))
    assert set(DELIVERY_KPIS) <= delivery


def test_une_seule_date_de_livraison_ne_produit_pas_de_visuel_de_livraison():
    only_promised = titles(purchasing_suppliers("ds-1", promised_date="date_promise"))
    assert not set(DELIVERY_KPIS) & only_promised


def _table(spec: dict, title: str) -> dict:
    return next(c["visual"] for c in spec["components"] if c["visual"].get("title") == title)


def test_noms_de_colonnes_invalides():
    for kwargs in ({"supplier": "a b"}, {"amount": "1x"}, {"date": "Date"}, {"quantity": "x-y"}, {"item": ""}):
        with pytest.raises(ValueError, match="not a valid column name"):
            purchasing_suppliers("ds-1", **kwargs)


def test_descripteur():
    assert DESCRIPTOR["id"] == "purchasing_suppliers" and DESCRIPTOR["version"] == 1 and DESCRIPTOR["params"] == []
    assert DESCRIPTOR["label"] == "Achats et fournisseurs"
    assert {"id", "label", "description", "version", "columns", "params"} == set(DESCRIPTOR)
    cols = {c["key"]: c for c in DESCRIPTOR["columns"]}
    assert set(cols) == {"date", "supplier", "item", "quantity", "amount", "promised_date", "received_date"}
    assert all(set(c) == {"key", "label", "type", "required", "default"} for c in cols.values())
    assert {k for k, c in cols.items() if c["required"]} == {"date", "supplier", "item", "amount"}
    assert cols["amount"]["type"] == "number" and cols["promised_date"]["type"] == "date"
    assert cols["supplier"]["type"] == "string" and cols["quantity"]["default"] is None


def test_build_appelle_le_gabarit_par_mots_cles():
    columns = {c["key"]: c["default"] for c in DESCRIPTOR["columns"]}
    assert build("ds-1", columns, {}) == purchasing_suppliers("ds-1")
    columns.update(quantity="quantite", supplier="vendeur")
    assert build("ds-1", columns, {}) == purchasing_suppliers("ds-1", quantity="quantite", supplier="vendeur")
    columns["quantity"] = ""
    assert build("ds-1", columns, {}) == purchasing_suppliers("ds-1", supplier="vendeur")


def test_build_colonne_requise_vide():
    columns = {c["key"]: c["default"] for c in DESCRIPTOR["columns"]}
    columns["supplier"] = None
    with pytest.raises(ValueError, match="not a valid column name"):
        build("ds-1", columns, {})


# ---------------------------------------------------------------------------
# Intégration sans mock du moteur
# ---------------------------------------------------------------------------


def test_integration_moteur_sans_mock(monkeypatch):
    df = _synthetic_purchases()
    dash, resp = compute(monkeypatch, purchasing_suppliers("ds-1", **FULL), df)
    assert errors(resp) == {}
    k = kpis(dash, resp)
    assert list(k) == BASE_KPIS + DELIVERY_KPIS
    exp = _expected(df)
    assert k["Dépenses totales"].value == pytest.approx(exp["depenses"])
    assert k["Fournisseurs"].value == exp["fournisseurs"] == 15
    assert k["Part du top 5"].value == pytest.approx(exp["top5"])
    assert 0.5 < exp["top5"] < 1 and k["Part du top 5"].status in ("warn", "bad")
    assert k["Livré à l'heure"].value == pytest.approx(exp["a_l_heure"])
    assert k["Retard moyen"].value == pytest.approx(exp["retard_moyen"])
    assert k["Dépenses totales"].spark is not None and len(k["Dépenses totales"].spark) >= 2


def test_part_du_top5_suit_les_filtres(monkeypatch):
    df = _synthetic_purchases()
    dash, resp = compute(monkeypatch, purchasing_suppliers("ds-1"), df, {"article": "Article 03", "periode": "6m"})
    assert errors(resp) == {}
    w = _window(df, 6)
    w = w[w["article"] == "Article 03"]
    by_supplier = w.groupby("fournisseur")["montant"].sum().sort_values(ascending=False)
    assert kpis(dash, resp)["Part du top 5"].value == pytest.approx(by_supplier.head(5).sum() / by_supplier.sum())
    assert kpis(dash, resp)["Dépenses totales"].value == pytest.approx(w["montant"].sum())


def test_graphiques_et_tableaux(monkeypatch):
    df = _synthetic_purchases()
    dash, resp = compute(monkeypatch, purchasing_suppliers("ds-1", **FULL), df)
    exp = _expected(df)
    _, monthly = component(dash, resp, "Dépenses par mois")
    assert len(monthly.rows) >= 12
    assert sum(r["depenses"] for r in monthly.rows) == pytest.approx(exp["depenses"])
    _, hbar = component(dash, resp, "Dépenses par fournisseur")
    assert [r["fournisseur"] for r in hbar.rows] == list(exp["by_supplier"].index[:10])
    _, table = component(dash, resp, "Détail par fournisseur")
    total = next(r for r in table.rows if r.get("_total"))
    rows = [r for r in table.rows if not r.get("_total")]
    assert len(rows) == 15 and total["depenses"] == pytest.approx(exp["depenses"])
    assert total["a_l_heure"] == pytest.approx(exp["a_l_heure"])
    _, articles = component(dash, resp, "Détail par article")
    w = _window(df)
    sums = w.groupby("article")[["montant", "quantite"]].sum()
    price = sums["montant"] / sums["quantite"]
    got = {r["article"]: r["prix_moyen"] for r in articles.rows}
    assert len(got) == 15  # top 15 par dépenses
    for name, value in got.items():
        assert value == pytest.approx(price[name])
    spend = w.groupby("article")["montant"].sum().sort_values(ascending=False)
    assert [r["article"] for r in articles.rows] == list(spend.index[:15])


def test_sans_colonnes_optionnelles(monkeypatch):
    df = _synthetic_purchases().drop(columns=["quantite", "date_promise", "date_reception"])
    dash, resp = compute(monkeypatch, purchasing_suppliers("ds-1"), df)
    assert errors(resp) == {}
    assert list(kpis(dash, resp)) == BASE_KPIS
    assert all(v.value is not None for v in kpis(dash, resp).values())
    _, articles = component(dash, resp, "Détail par article")
    assert "prix_moyen" not in articles.rows[0]


def test_colonnes_renommees(monkeypatch):
    df = _synthetic_purchases().rename(columns={
        "date": "jour", "fournisseur": "vendeur", "article": "famille", "quantite": "qte", "montant": "ht",
        "date_promise": "promis", "date_reception": "recu",
    })
    spec = purchasing_suppliers(
        "ds-1", date="jour", supplier="vendeur", item="famille", quantity="qte", amount="ht",
        promised_date="promis", received_date="recu",
    )
    assert validate_spec(dashboard_from_spec(spec)) == []
    dash, resp = compute(monkeypatch, spec, df)
    assert errors(resp) == {}
    k = kpis(dash, resp)
    assert list(k) == BASE_KPIS + DELIVERY_KPIS
    assert k["Part du top 5"].value == pytest.approx(_expected(_synthetic_purchases())["top5"])


def test_moins_de_cinq_fournisseurs_donne_100_pourcent(monkeypatch):
    df = _synthetic_purchases()
    df["fournisseur"] = df["fournisseur"].where(df["fournisseur"] < "Fournisseur 03", "Fournisseur 00")
    dash, resp = compute(monkeypatch, purchasing_suppliers("ds-1"), df)
    assert errors(resp) == {}
    assert kpis(dash, resp)["Part du top 5"].value == pytest.approx(1.0)


def test_aucune_reception_ne_casse_pas(monkeypatch):
    df = _synthetic_purchases()
    df["date_reception"] = pd.NaT
    dash, resp = compute(monkeypatch, purchasing_suppliers("ds-1", **FULL), df)
    assert errors(resp) == {}
    assert kpis(dash, resp)["Livré à l'heure"].value is None

"""Gabarit « Livraisons / OTIF » : spec valide, grille sans chevauchement, mesures
existantes, moteur réel sur un jeu synthétique (colonnes par défaut, renommées, sans optionnelles)."""
import asyncio
import random
import re

import pandas as pd
import pytest

from apowerb.bi.dashboards.core import ComponentType, Dashboard, DashboardComponent, GridPosition
from apowerb.bi.spec.models import DashboardDataRequest, FilterSpec, SourceSpec, VisualSpec
from apowerb.bi.spec.templates_otif import DESCRIPTOR, build, delivery_otif
from apowerb.bi.spec.validate import validate_spec

KPI_TITLES = ["OTIF", "Livré à l'heure", "Livré complet", "Retard moyen", "En retard non livrées"]


def _dashboard(spec: dict) -> Dashboard:
    return Dashboard.create(
        "Livraisons",
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


def _synthetic_deliveries() -> pd.DataFrame:
    rnd = random.Random(11)
    today = pd.Timestamp("2025-10-31")
    customers = [f"Client {i:02d}" for i in range(15)]
    carriers = ["Transporteur A", "Transporteur B", "Transporteur C"]
    families = ["Pièces", "Fluides", "Emballages", "Outillage"]
    rows = []
    for _ in range(2000):
        ordered = pd.Timestamp("2024-09-01") + pd.Timedelta(days=rnd.randrange(0, 425))
        promised = ordered + pd.Timedelta(days=rnd.choice([5, 7, 10]))
        qty = rnd.randrange(10, 200)
        roll = rnd.random()
        delay = 0
        if roll < 0.08:
            delay = rnd.choice([1, 2, 2, 4, 5, 8, 12, 15])
        elif roll < 0.4:
            delay = -rnd.choice([0, 1, 2])
        delivered = promised + pd.Timedelta(days=delay)
        qty_delivered = qty if rnd.random() >= 0.04 else int(qty * rnd.uniform(0.5, 0.95))
        if delivered > today or rnd.random() < 0.006:
            delivered, qty_delivered = pd.NaT, None
        rows.append({
            "date_commande": ordered,
            "date_promise": promised,
            "date_livraison": delivered,
            "qte_commandee": qty,
            "qte_livree": qty_delivered,
            "client": rnd.choice(customers),
            "transporteur": rnd.choice(carriers),
            "famille": rnd.choice(families),
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


FULL = dict(carrier="transporteur", family="famille")


@pytest.fixture(params=[{}, FULL], ids=["sans_optionnelles", "complet"])
def spec(request):
    return delivery_otif("ds-1", **request.param)


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
        refs |= {c["key"] for c in v.get("columns", [])} - {"client"}
        if v["type"] == "text":
            refs |= {r for r in tmpl.findall(v["text"]) if r not in ("filters", "period")}
        assert refs <= measures | fields, (v.get("title"), refs - measures - fields)


def test_seuils_et_ligne_de_reference():
    spec = delivery_otif("ds-1")
    m = {x["name"]: x for x in spec["sources"][0]["measures"]}
    for name in ("otif", "a_l_heure", "complet"):
        assert m[name]["thresholds"] == {"direction": "higher_is_better", "good": 0.95, "warn": 0.9}
        assert m[name]["format"] == "percent"
    combo = next(c["visual"] for c in spec["components"] if c["visual"]["type"] == "combo")
    assert [r["value"] for r in combo["reference_lines"]] == [0.95]


def test_composants_conditionnels():
    titles = lambda s: {c["visual"].get("title") for c in s["components"]}  # noqa: E731
    base = titles(delivery_otif("ds-1"))
    assert "OTIF par transporteur" not in base and "OTIF par famille" not in base
    both = titles(delivery_otif("ds-1", **FULL))
    assert {"OTIF par transporteur", "OTIF par famille"} <= both
    ids = {f["id"] for f in delivery_otif("ds-1", **FULL)["filters"]}
    assert ids == {"periode", "client", "transporteur", "famille"}
    assert {f["id"] for f in delivery_otif("ds-1")["filters"]} == {"periode", "client"}


def test_filtre_periode_sur_la_date_promise():
    spec = delivery_otif("ds-1", promised_date="promise", default_period="26w")
    periode = next(f for f in spec["filters"] if f["id"] == "periode")
    assert periode["field"] == "promise" and periode["default"] == "26w"


@pytest.mark.parametrize(
    "kwargs",
    [{"order_date": "Date"}, {"customer": "a b"}, {"qty_ordered": "1x"}, {"carrier": "Transporteur"}, {"family": "x-y"}],
)
def test_noms_de_colonnes_invalides(kwargs):
    with pytest.raises(ValueError, match="not a valid column name"):
        delivery_otif("ds-1", **kwargs)


def test_descripteur():
    assert DESCRIPTOR["id"] == "delivery_otif" and DESCRIPTOR["version"] == 1 and DESCRIPTOR["params"] == []
    assert {"id", "label", "description", "version", "columns", "params"} == set(DESCRIPTOR)
    cols = {c["key"]: c for c in DESCRIPTOR["columns"]}
    assert set(cols) == {
        "order_date", "promised_date", "delivered_date", "qty_ordered", "qty_delivered", "customer", "carrier", "family",
    }
    assert all(set(c) == {"key", "label", "type", "required", "default"} for c in cols.values())
    assert {k for k, c in cols.items() if not c["required"]} == {"carrier", "family"}
    assert cols["promised_date"]["type"] == "date" and cols["qty_ordered"]["type"] == "number"
    assert cols["customer"]["type"] == "string" and cols["customer"]["default"] == "client"
    assert cols["carrier"]["default"] is None


def test_build_appelle_le_gabarit_par_mots_cles():
    columns = {c["key"]: c["default"] for c in DESCRIPTOR["columns"]}
    assert build("ds-1", columns, {}) == delivery_otif("ds-1")
    columns.update(carrier="transporteur", customer="acheteur")
    assert build("ds-1", columns, {}) == delivery_otif("ds-1", carrier="transporteur", customer="acheteur")
    columns["carrier"] = ""
    assert build("ds-1", columns, {}) == delivery_otif("ds-1", customer="acheteur")


def test_build_colonne_requise_vide():
    columns = {c["key"]: c["default"] for c in DESCRIPTOR["columns"]}
    columns["customer"] = None
    with pytest.raises(ValueError, match="not a valid column name"):
        build("ds-1", columns, {})


# ---------------------------------------------------------------------------
# Intégration sans mock du moteur
# ---------------------------------------------------------------------------


def _expected(df: pd.DataFrame, months: int = 12, client: str | None = None) -> dict:
    last = df["date_promise"].max()
    as_of = df["date_livraison"].max()
    window = df[df["date_promise"] > last - pd.DateOffset(months=months)]
    if client:
        window = window[window["client"] == client]
    done = window[window["date_livraison"].notna()]
    on_time = done["date_livraison"] <= done["date_promise"]
    complete = done["qte_livree"] >= done["qte_commandee"]
    late_days = (done["date_livraison"] - done["date_promise"]).dt.days[~on_time]
    open_late = window[window["date_livraison"].isna() & (window["date_promise"] < as_of)]
    return {
        "otif": float((on_time & complete).mean()),
        "a_l_heure": float(on_time.mean()),
        "complet": float(complete.mean()),
        "retard_moyen": float(late_days.mean()),
        "retards_en_cours": float(len(open_late)),
        "livrees": len(done),
    }


def test_integration_moteur_sans_mock(monkeypatch):
    df = _synthetic_deliveries()
    dash, resp = _compute(monkeypatch, delivery_otif("ds-1", **FULL), df)
    broken = {cid: r.error for cid, r in resp.components.items() if r.error}
    assert broken == {}
    kpis = _kpis(dash, resp)
    assert list(kpis) == KPI_TITLES
    assert all(v.value is not None for v in kpis.values())
    exp = _expected(df)
    assert 0.80 < kpis["OTIF"].value < 0.95
    assert kpis["OTIF"].value == pytest.approx(exp["otif"])
    assert kpis["Livré à l'heure"].value == pytest.approx(exp["a_l_heure"])
    assert kpis["Livré complet"].value == pytest.approx(exp["complet"])
    assert kpis["Retard moyen"].value == pytest.approx(exp["retard_moyen"])
    assert kpis["En retard non livrées"].value == exp["retards_en_cours"] > 0
    assert kpis["OTIF"].status in ("warn", "bad")
    assert kpis["OTIF"].spark is not None and len(kpis["OTIF"].spark) >= 2
    assert "Taux de service quantité" in (kpis["Livré complet"].secondary or "")
    print({k: v.formatted for k, v in kpis.items()})


def test_ligne_toutes_du_tableau_egale_aux_kpi(monkeypatch):
    df = _synthetic_deliveries()
    dash, resp = _compute(monkeypatch, delivery_otif("ds-1", **FULL), df)
    kpis = _kpis(dash, resp)
    _, table = _component(dash, resp, "Performance par client")
    total = next(r for r in table.rows if r.get("_total"))
    clients = [r for r in table.rows if not r.get("_total")]
    assert len(clients) == 15
    assert total["client"] == "Toutes"
    assert total["otif"] == pytest.approx(kpis["OTIF"].value)
    assert total["a_l_heure"] == pytest.approx(kpis["Livré à l'heure"].value)
    assert total["complet"] == pytest.approx(kpis["Livré complet"].value)
    assert total["retard_moyen"] == pytest.approx(kpis["Retard moyen"].value)
    assert total["lignes"] == sum(r["lignes"] for r in clients)
    # la moyenne simple des clients n'est pas la valeur globale
    assert total["otif"] != pytest.approx(sum(r["otif"] for r in clients) / len(clients), abs=1e-9)


def test_graphiques(monkeypatch):
    df = _synthetic_deliveries()
    dash, resp = _compute(monkeypatch, delivery_otif("ds-1", **FULL), df)
    _, par_client = _component(dash, resp, "OTIF par client")
    values = [r["otif"] for r in par_client.rows]
    assert len(values) == 15 and values == sorted(values)  # pires en premier
    _, par_transporteur = _component(dash, resp, "OTIF par transporteur")
    assert len(par_transporteur.rows) == 3
    _, par_famille = _component(dash, resp, "OTIF par famille")
    assert len(par_famille.rows) == 4
    _, retards = _component(dash, resp, "Distribution des retards")
    tranches = [r["x"] for r in retards.rows]
    assert tranches == ["0 j", "1-2 j", "3-5 j", "6-10 j", "> 10 j"]
    total_livrees = sum(r["lignes_livrees"] for r in retards.rows)
    assert total_livrees == _expected(df)["livrees"]
    zero = retards.rows[0]["lignes_livrees"]
    assert zero / total_livrees == pytest.approx(_kpis(dash, resp)["Livré à l'heure"].value)
    _, evolution = _component(dash, resp, "OTIF et livraisons à l'heure")
    assert len(evolution.rows) >= 12
    assert evolution.reference_lines[0]["value"] == 0.95


def test_filtre_client(monkeypatch):
    df = _synthetic_deliveries()
    dash, resp = _compute(monkeypatch, delivery_otif("ds-1", **FULL), df, {"client": "Client 03"})
    assert {cid: r.error for cid, r in resp.components.items() if r.error} == {}
    expected = _expected(df, client="Client 03")
    kpis = _kpis(dash, resp)
    assert kpis["OTIF"].value == pytest.approx(expected["otif"])
    assert kpis["En retard non livrées"].value == expected["retards_en_cours"]


def test_colonnes_renommees(monkeypatch):
    df = _synthetic_deliveries().rename(columns={
        "date_commande": "cmd", "date_promise": "promesse", "date_livraison": "livre_le", "qte_commandee": "q_cmd",
        "qte_livree": "q_liv", "client": "acheteur", "transporteur": "trans", "famille": "gamme",
    })
    spec = delivery_otif(
        "ds-1", order_date="cmd", promised_date="promesse", delivered_date="livre_le", qty_ordered="q_cmd",
        qty_delivered="q_liv", customer="acheteur", carrier="trans", family="gamme",
    )
    assert validate_spec(_dashboard(spec)) == []
    dash, resp = _compute(monkeypatch, spec, df)
    assert {cid: r.error for cid, r in resp.components.items() if r.error} == {}
    kpis = _kpis(dash, resp)
    assert all(v.value is not None for v in kpis.values())
    reference = _expected(_synthetic_deliveries())
    assert kpis["OTIF"].value == pytest.approx(reference["otif"])


def test_sans_colonnes_optionnelles(monkeypatch):
    df = _synthetic_deliveries().drop(columns=["transporteur", "famille"])
    spec = delivery_otif("ds-1")
    dash, resp = _compute(monkeypatch, spec, df)
    assert {cid: r.error for cid, r in resp.components.items() if r.error} == {}
    assert all(v.value is not None for v in _kpis(dash, resp).values())
    titles = {c.visual.title for c in dash.components}
    assert "OTIF par transporteur" not in titles and "OTIF par famille" not in titles


def test_jeu_sans_aucune_livraison_en_retard_ne_casse_pas(monkeypatch):
    df = _synthetic_deliveries()
    df["date_livraison"] = df["date_promise"]
    df["qte_livree"] = df["qte_commandee"]
    dash, resp = _compute(monkeypatch, delivery_otif("ds-1"), df)
    assert {cid: r.error for cid, r in resp.components.items() if r.error} == {}
    kpis = _kpis(dash, resp)
    assert kpis["OTIF"].value == 1.0 and kpis["OTIF"].status == "good"


def test_colonne_nommee_comme_le_regroupement_temporel(monkeypatch):
    df = _synthetic_deliveries().rename(columns={"date_promise": "mois"})
    dash, resp = _compute(monkeypatch, delivery_otif("ds-1", promised_date="mois"), df)
    assert {cid: r.error for cid, r in resp.components.items() if r.error} == {}
    assert _kpis(dash, resp)["OTIF"].value == pytest.approx(_expected(_synthetic_deliveries())["otif"])

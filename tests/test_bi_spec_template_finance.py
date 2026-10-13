"""Gabarit « Pilotage financier » sur un dataset FEC : descripteur, spec valide,
et calcul de bout en bout sur un petit FEC synthétique (valeurs calculées à la main)."""
import asyncio

import pandas as pd
import pytest

from apowerb.bi.dashboards.core import ComponentType, Dashboard, DashboardComponent, GridPosition
from apowerb.bi.spec import templates_finance as tf
from apowerb.bi.spec.catalog import DashboardTemplate, TemplateColumn, TemplateParam
from apowerb.bi.spec.models import DashboardDataRequest, FilterSpec, SourceSpec, VisualSpec
from apowerb.bi.spec.validate import validate_spec

FEC_COLUMNS = [
    "journalcode", "journallib", "ecriturenum", "ecrituredate", "comptenum", "comptelib", "compauxnum",
    "compauxlib", "pieceref", "piecedate", "ecriturelib", "debit", "credit", "ecriturelet", "datelet",
    "validdate", "montantdevise", "idevise",
]
REQUIRED = {"ecrituredate", "comptenum", "debit", "credit"}
FEC_MAPPING = {k: k for k in FEC_COLUMNS}


# --------------------------------------------------------------------------- helpers

def _template() -> DashboardTemplate:
    return DashboardTemplate(
        id=tf.DESCRIPTOR["id"],
        label=tf.DESCRIPTOR["label"],
        description=tf.DESCRIPTOR["description"],
        columns=tuple(TemplateColumn(**c) for c in tf.DESCRIPTOR["columns"]),
        params=tuple(TemplateParam(**p) for p in tf.DESCRIPTOR["params"]),
        build=tf.build,
    )


def _dashboard(spec: dict) -> Dashboard:
    return Dashboard.create(
        "Pilotage financier",
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


# Mini FEC (date, compte, libellé, débit, crédit). Chaque écriture est équilibrée.
_ROWS = [
    # --- janvier 2024
    ("AN", "2024-01-01", "512000", "Banque", 10000, 0), ("AN", "2024-01-01", "411000", "Clients", 3000, 0),
    ("AN", "2024-01-01", "370000", "Stocks de marchandises", 1500, 0),
    ("AN", "2024-01-01", "401000", "Fournisseurs", 0, 2000), ("AN", "2024-01-01", "101000", "Capital", 0, 12500),
    ("VT", "2024-01-15", "411000", "Clients", 12000, 0), ("VT", "2024-01-15", "701000", "Ventes de produits", 0, 10000),
    ("VT", "2024-01-15", "445710", "TVA collectée", 0, 2000),
    ("HA", "2024-01-10", "601000", "Achats de matières", 4000, 0), ("HA", "2024-01-10", "445660", "TVA déductible", 800, 0),
    ("HA", "2024-01-10", "401000", "Fournisseurs", 0, 4800),
    ("OD", "2024-01-31", "641000", "Salaires", 3000, 0), ("OD", "2024-01-31", "421000", "Personnel", 0, 3000),
    ("BQ", "2024-01-31", "421000", "Personnel", 3000, 0), ("BQ", "2024-01-31", "512000", "Banque", 0, 3000),
    ("BQ", "2024-01-20", "512000", "Banque", 5000, 0), ("BQ", "2024-01-20", "411000", "Clients", 0, 5000),
    ("BQ", "2024-01-25", "401000", "Fournisseurs", 2000, 0), ("BQ", "2024-01-25", "512000", "Banque", 0, 2000),
    ("BQ", "2024-01-12", "622000", "Honoraires", 600, 0), ("BQ", "2024-01-12", "512000", "Banque", 0, 600),
    ("OD", "2024-01-31", "370000", "Stocks de marchandises", 500, 0), ("OD", "2024-01-31", "603000", "Variation de stocks", 0, 500),
    # --- février 2024
    ("VT", "2024-02-10", "411000", "Clients", 6000, 0), ("VT", "2024-02-10", "706000", "Prestations de services", 0, 5000),
    ("VT", "2024-02-10", "445710", "TVA collectée", 0, 1000),
    ("BQ", "2024-02-15", "512000", "Banque", 200, 0), ("BQ", "2024-02-15", "758000", "Produits divers", 0, 200),
    ("BQ", "2024-02-05", "607000", "Achats de marchandises", 2500, 0), ("BQ", "2024-02-05", "512000", "Banque", 0, 2500),
    ("BQ", "2024-02-28", "641000", "Salaires", 3000, 0), ("BQ", "2024-02-28", "512000", "Banque", 0, 3000),
    ("BQ", "2024-02-20", "635000", "Impôts et taxes", 300, 0), ("BQ", "2024-02-20", "512000", "Banque", 0, 300),
    ("BQ", "2024-02-28", "661000", "Intérêts d'emprunt", 100, 0), ("BQ", "2024-02-28", "512000", "Banque", 0, 100),
    # --- mars 2024
    ("VT", "2024-03-12", "411000", "Clients", 24000, 0), ("VT", "2024-03-12", "707000", "Ventes de marchandises", 0, 20000),
    ("VT", "2024-03-12", "445710", "TVA collectée", 0, 4000),
    ("BQ", "2024-03-25", "512000", "Banque", 6000, 0), ("BQ", "2024-03-25", "411000", "Clients", 0, 6000),
    ("HA", "2024-03-05", "601000", "Achats de matières", 6000, 0), ("HA", "2024-03-05", "401000", "Fournisseurs", 0, 6000),
    ("OD", "2024-03-31", "603000", "Variation de stocks", 700, 0), ("OD", "2024-03-31", "370000", "Stocks de marchandises", 0, 700),
    ("BQ", "2024-03-31", "641000", "Salaires", 3000, 0), ("BQ", "2024-03-31", "512000", "Banque", 0, 3000),
    ("BQ", "2024-03-15", "658000", "Charges diverses", 150, 0), ("BQ", "2024-03-15", "512000", "Banque", 0, 150),
    ("BQ", "2024-03-20", "512000", "Banque", 400, 0), ("BQ", "2024-03-20", "777000", "Produits exceptionnels", 0, 400),
    ("OD", "2024-03-31", "681000", "Dotations aux amortissements", 250, 0), ("OD", "2024-03-31", "281000", "Amortissements", 0, 250),
]


def _fec() -> pd.DataFrame:
    df = pd.DataFrame(
        {
            "journalcode": [r[0] for r in _ROWS],
            "ecrituredate": pd.to_datetime([r[1] for r in _ROWS]),
            "comptenum": [r[2] for r in _ROWS],
            "comptelib": [r[3] for r in _ROWS],
            "debit": [float(r[4]) for r in _ROWS],
            "credit": [float(r[5]) for r in _ROWS],
        }
    )
    df["ecriturenum"] = range(1, len(df) + 1)
    for col in FEC_COLUMNS:
        if col not in df.columns:
            df[col] = None
    return df[FEC_COLUMNS]


@pytest.fixture
def computed(monkeypatch):
    import apowerb.bi.spec.sources as sources
    import apowerb.bi.spec.transforms as transforms
    from apowerb.bi.spec.engine import compute_dashboard

    async def fake_load(dataset_id, owner, sheet=None):
        return _fec()

    def run(filters=None, **params):
        transforms.clear_cache()
        monkeypatch.setattr(sources, "load_dataset", fake_load)
        spec = tf.build("ds-1", dict(FEC_MAPPING), params)
        dash = _dashboard(spec)
        resp = asyncio.run(
            compute_dashboard(dash, DashboardDataRequest(filters=filters or {}), owner="alice@example.com")
        )
        broken = {c.visual.title: resp.components[c.id].error for c in dash.components if resp.components[c.id].error}
        assert broken == {}
        return dash, resp

    return run


def _kpis(dash, resp):
    return {c.visual.kpi.measure: resp.components[c.id].value.value for c in dash.components if c.visual.type == "kpi"}


def _rows(dash, resp, kind, title_part):
    for c in dash.components:
        if c.visual.type == kind and title_part in (c.visual.title or ""):
            return resp.components[c.id].rows
    raise AssertionError(f"visuel introuvable : {kind} {title_part}")


# --------------------------------------------------------------------------- descripteur

def test_descriptor_conforme_au_contrat():
    d = tf.DESCRIPTOR
    assert set(d) == {"id", "label", "description", "version", "columns", "params"}
    assert d["id"] == "finance_pilotage" and d["version"] == 1
    assert [c["key"] for c in d["columns"]] == FEC_COLUMNS
    for c in d["columns"]:
        assert set(c) == {"key", "label", "type", "required", "default"}
        assert c["type"] in ("date", "string", "number")
        assert c["default"] == c["key"]
        assert c["required"] is (c["key"] in REQUIRED)
    types = {c["key"]: c["type"] for c in d["columns"]}
    assert types["ecrituredate"] == "date" and types["debit"] == "number" and types["comptenum"] == "string"
    keys = [p["key"] for p in d["params"]]
    assert len(keys) == len(set(keys)) >= 4
    for p in d["params"]:
        assert set(p) == {"key", "label", "type", "default", "min", "max"}
        assert p["type"] == "integer" and p["min"] <= p["default"] <= p["max"]


def test_descripteur_accepte_par_le_catalogue():
    tpl = _template()
    cols, errors = tpl.clean_columns(dict(FEC_MAPPING))
    assert errors == [] and cols["comptenum"] == "comptenum"
    params, errors = tpl.clean_params({})
    assert errors == [] and set(params) == {p.key for p in tpl.params}
    _, errors = tpl.clean_params({"marge_brute_cible_pct": 101})
    assert errors


# --------------------------------------------------------------------------- spec

def test_spec_valide_et_sans_chevauchement():
    spec = tf.build("ds-1", dict(FEC_MAPPING), {})
    assert validate_spec(_dashboard(spec)) == []
    assert spec["sources"][0]["kind"] == "dataset" and spec["sources"][0]["dataset_id"] == "ds-1"
    assert [f["type"] for f in spec["filters"]] == ["relative_period"]
    kinds = {c["visual"]["type"] for c in spec["components"]}
    assert {"kpi", "combo", "waterfall", "hbar", "table", "text", "note"} <= kinds
    taken = set()
    for comp in spec["components"]:
        p = comp["position"]
        assert p["col"] >= 0 and p["col"] + p["width"] <= 12
        cells = {(r, c) for r in range(p["row"], p["row"] + p["height"]) for c in range(p["col"], p["col"] + p["width"])}
        assert not cells & taken
        taken |= cells


def test_params_reglables_dans_la_spec():
    spec = tf.build("ds-1", dict(FEC_MAPPING), {"periode_defaut_mois": 6, "tresorerie_min": 5000, "marge_brute_cible_pct": 40})
    assert spec["filters"][0]["default"] == "6m"
    assert validate_spec(_dashboard(spec)) == []
    treso = next(c["visual"] for c in spec["components"] if c["visual"]["type"] == "area")
    assert treso["reference_lines"][0]["value"] == 5000
    measures = {m["name"]: m for m in spec["sources"][1]["measures"]}
    assert measures["taux_marge_brute"]["thresholds"]["good"] == pytest.approx(0.40)
    assert measures["tresorerie"]["thresholds"]["good"] == 5000


@pytest.mark.parametrize("missing", sorted(REQUIRED))
def test_colonne_obligatoire_manquante(missing):
    columns = dict(FEC_MAPPING)
    columns[missing] = None
    with pytest.raises(ValueError, match=missing):
        tf.build("ds-1", columns, {})
    columns[missing] = ""
    with pytest.raises(ValueError, match=missing):
        tf.build("ds-1", columns, {})
    del columns[missing]
    with pytest.raises(ValueError, match=missing):
        tf.build("ds-1", columns, {})


def test_colonne_invalide_et_parametre_hors_bornes():
    with pytest.raises(ValueError, match="not a valid column name"):
        tf.build("ds-1", {**FEC_MAPPING, "debit": "Debit €"}, {})
    with pytest.raises(ValueError, match="marge_brute_cible_pct"):
        tf.build("ds-1", dict(FEC_MAPPING), {"marge_brute_cible_pct": 150})
    with pytest.raises(ValueError, match="dataset_id"):
        tf.build("", dict(FEC_MAPPING), {})


# --------------------------------------------------------------------------- calcul de bout en bout

def test_indicateurs_calcules_a_la_main(computed):
    dash, resp = computed()
    k = _kpis(dash, resp)
    # Trésorerie = Σ(débit − crédit) des 512 : janv 10000-3000+5000-2000-600 = 9400 ; févr -5700 ; mars +3250.
    assert k["tresorerie"] == 6950
    # CA (70) : 10000 + 5000 + 20000 ; dernier mois (mars) : 20000.
    assert k["ca"] == 35000 and k["ca_mois"] == 20000
    # Achats 60 (603 inclus) : 3500 + 2500 + 6700 = 12700 ; marge brute = 35000 - 12700.
    assert k["taux_marge_brute"] == pytest.approx(22300 / 35000)
    # EBE : produits 70-75 (35200) − charges 60-65 (12700+600+300+9000+150 = 22750).
    assert k["ebe"] == 12450
    assert k["charges_personnel"] == 9000
    # BFR : stocks 1300 + clients 34000 − fournisseurs 10800.
    assert k["bfr"] == 24500
    # Seuil : fixes (22750-12700=10050) ÷ taux de marge sur coûts variables (22300/35000).
    assert k["seuil_rentabilite"] == pytest.approx(10050 * 35000 / 22300)


def test_seuil_avec_hypothese_fixes_variables_reglee(computed):
    dash, resp = computed(part_variable_personnel_pct=100)
    # variables = 12700 + 9000 ; fixes = 22750 - 21700 = 1050 ; MCV = 13300.
    assert _kpis(dash, resp)["seuil_rentabilite"] == pytest.approx(1050 * 35000 / 13300)
    dash, resp = computed(part_variable_achats_pct=50)
    assert _kpis(dash, resp)["seuil_rentabilite"] == pytest.approx((22750 - 6350) * 35000 / (35000 - 6350))


def test_graphiques_mensuels_et_cascade(computed):
    dash, resp = computed()
    treso = _rows(dash, resp, "area", "résorerie")
    assert [r["tresorerie"] for r in treso] == [9400, 3700, 6950]
    combo = _rows(dash, resp, "combo", "")
    assert [r["ca"] for r in combo] == [10000, 5000, 20000]
    assert [r["marge_brute"] for r in combo] == [6500, 2500, 13300]
    steps = {r["label"]: r for r in _rows(dash, resp, "waterfall", "")}
    ends = [r for r in _rows(dash, resp, "waterfall", "") if r["kind"] == "end"]
    assert [r["value"] for r in ends] == [12450, 12500]  # EBE puis résultat net
    assert steps["Chiffre d'affaires"]["value"] == 35000
    assert steps["Achats consommés"]["value"] == 12700
    # la cascade se referme : aucun avertissement d'écart entre étapes et totaux
    wf = next(c for c in dash.components if c.visual.type == "waterfall")
    assert resp.components[wf.id].meta.warnings == []
    top = _rows(dash, resp, "hbar", "")
    assert top[0]["x"].startswith("601000") and top[0]["charges_par_compte"] == 10000
    assert top[1]["x"].startswith("641000") and top[1]["charges_par_compte"] == 9000
    table = _rows(dash, resp, "table", "")
    march = next(r for r in table if str(r["periode_mois"]).startswith("2024-03"))
    assert march["ca"] == 20000 and march["tresorerie"] == 6950 and march["bfr"] == 24500


def test_filtre_periode_garde_les_soldes_de_fin_de_periode(computed):
    # 2 derniers mois (févr. + mars) : flux sur la période, soldes cumulés depuis l'ouverture.
    dash, resp = computed(filters={"periode": "2m"})
    k = _kpis(dash, resp)
    assert k["ca"] == 25000 and k["ca_mois"] == 20000
    assert k["tresorerie"] == 6950 and k["bfr"] == 24500
    assert k["taux_marge_brute"] == pytest.approx((25000 - 9200) / 25000)
    assert [r["tresorerie"] for r in _rows(dash, resp, "area", "résorerie")] == [3700, 6950]


def test_texte_de_synthese_rendu(computed):
    dash, resp = computed()
    text = next(resp.components[c.id].text for c in dash.components if c.visual.type == "text")
    assert "{{" not in text and "35" in text

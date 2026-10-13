"""Étape « replenishment_plan » : stock réel projeté sur l'horizon, commande
passée quand le stock à l'arrivée d'une commande passerait sous le seuil."""
import asyncio

import numpy as np
import pandas as pd
import pytest

from apowerb.bi.spec.models import Provenance, ReplenishmentPlanStep
from apowerb.bi.spec.transforms import (
    PLAN_COLUMNS,
    TransformState,
    apply_transforms,
    plan_replenishment,
)


def _plan(stock0, forecast, *, seuil, lead, cover=4, lot=1):
    return plan_replenishment(
        float(stock0), np.asarray(forecast, dtype=float), seuil=seuil, lead_time=lead, target_cover=cover, lot=lot
    )


# ---------------------------------------------------------------------------
# Règle de commande (calcul à la main)
# ---------------------------------------------------------------------------


def test_commande_juste_a_temps_et_quantite_de_couverture():
    # Stock 100, seuil 40, délai 2, prévision 30/semaine : sans commande le
    # stock tombe à 10 en semaine 3 -> commander en semaine 1 de quoi revenir
    # au seuil + 4 semaines de prévision (40 + 120 - 10 = 150).
    p = _plan(100, [30] * 8, seuil=40, lead=2)
    assert p["commande"].tolist() == [150, 0, 0, 0, 0, 150, 0, 0]
    assert p["reception"].tolist() == [0, 0, 150, 0, 0, 0, 0, 150]
    assert p["stock_projete"].tolist() == [70, 40, 160, 130, 100, 70, 40, 160]
    assert p["stock_sans_commande"].tolist() == [70, 40, 10, 0, 0, 0, 0, 0]
    assert p["risque"].tolist() == [1, 0, 0, 0, 0, 1, 0, 0]


def test_pas_de_commande_quand_le_stock_suffit():
    p = _plan(10_000, [30] * 8, seuil=40, lead=2)
    assert p["commande"].sum() == 0
    assert p["risque"].max() == 0


def test_rupture_inevitable_pendant_le_delai():
    # Stock 50, délai 4 : rien ne peut arriver avant la semaine 5.
    p = _plan(50, [30] * 8, seuil=40, lead=4)
    assert p["stock_projete"].tolist()[:4] == [20, 0, 0, 0]
    assert p["risque"].tolist()[:4] == [2, 2, 2, 2]
    assert p["commande"][0] == 40 + 120 - 0
    assert p["reception"][4] == p["commande"][0]
    assert p["stock_projete"][4] == 130


def test_quantite_arrondie_au_lot():
    p = _plan(100, [30] * 8, seuil=40, lead=2, lot=100)
    assert p["commande"][0] == 200


def test_pas_de_commande_qui_arriverait_apres_l_horizon():
    # Horizon 4, délai 3 : seule la commande de la semaine 1 arrive dans
    # l'horizon (semaine 4) ; au-delà, la prévision de la dernière semaine est
    # répétée pour la couverture (40 + 4 x 30 - 0 = 160).
    p = _plan(100, [30] * 4, seuil=40, lead=3)
    assert p["commande"].tolist() == [160, 0, 0, 0]
    assert p["stock_projete"].tolist() == [70, 40, 10, 140]
    assert p["risque"].tolist() == [1, 0, 2, 0]


# ---------------------------------------------------------------------------
# Étape complète sur un tableau historique + futur
# ---------------------------------------------------------------------------


def _frame(with_future=True):
    rows = []
    start = pd.Timestamp("2025-01-06")
    for fam, stock_last, seuil, delai in [("A", 100, 40, 2), ("B", 10_000, 50, 1)]:
        for w in range(20):
            rows.append({"semaine": start + pd.Timedelta(weeks=w), "famille": fam, "ventes": 30.0,
                         "stock": 500.0 if w < 19 else float(stock_last), "seuil": seuil, "delai": delai})
        if with_future:
            for w in range(20, 28):
                rows.append({"semaine": start + pd.Timedelta(weeks=w), "famille": fam, "prev": 30.0})
    return pd.DataFrame(rows)


def _run(df, **kw):
    step = ReplenishmentPlanStep(date="semaine", by=["famille"], stock="stock", demand="ventes", forecast="prev",
                                 safety_column="seuil", lead_time_column="delai", **kw)
    state = TransformState(df=df, prov={c: Provenance.REAL for c in df.columns})
    return asyncio.run(apply_transforms(state, [step], ctx=None))


def test_etape_par_famille_avec_seuil_et_delai_du_fichier():
    out = _run(_frame(), keep_history=4).df
    a = out[out["famille"] == "A"].sort_values("semaine")
    assert len(a) == 4 + 8
    futur = a[a["semaine_plan"] >= 1]
    assert futur["semaine_plan"].tolist() == list(range(1, 9))
    assert futur["commande"].tolist() == [150, 0, 0, 0, 0, 150, 0, 0]
    assert set(futur["seuil_plan"]) == {40} and set(futur["delai_plan"]) == {2}
    actuel = a[a["semaine_plan"] == 0]
    assert len(actuel) == 1 and actuel["stock_projete"].iloc[0] == 100
    b = out[out["famille"] == "B"]
    assert b["commande"].sum() == 0


def test_seuil_et_delai_aussi_sur_l_historique_garde():
    # Le seuil se trace sur tout le graphique, pas seulement sur l'horizon.
    out = _run(_frame(), keep_history=4).df
    a = out[(out["famille"] == "A") & ~(out["semaine_plan"] >= 1)]
    assert len(a) == 4
    assert set(a["seuil_plan"]) == {40} and set(a["delai_plan"]) == {2}
    assert a["commande"].isna().all()


def test_seuil_sur_l_historique_en_repli_sans_prevision():
    out = _run(_frame(with_future=False), keep_history=4).df
    a = out[(out["famille"] == "A") & ~(out["semaine_plan"] >= 1)]
    assert len(a) == 4 and set(a["seuil_plan"]) == {40}


def test_provenances_et_colonnes():
    state = _run(_frame())
    for col in PLAN_COLUMNS:
        assert col in state.df.columns
    assert state.prov["stock_projete"] == Provenance.FORECAST
    assert state.prov["commande"] == Provenance.SIMULATED
    assert state.prov["stock"] == Provenance.REAL


def test_sans_prevision_moteur_repli_sur_la_moyenne_avec_avertissement():
    df = _frame(with_future=False)
    state = _run(df, horizon=8)
    a = state.df[state.df["famille"] == "A"]
    futur = a[a["semaine_plan"] >= 1].sort_values("semaine")
    assert len(futur) == 8
    assert (futur["prev"] == 30).all()
    assert futur["semaine"].iloc[0] == pd.Timestamp("2025-01-06") + pd.Timedelta(weeks=20)
    assert any("moyenne des 4 dernières semaines" in w for w in state.warnings)


def test_valeurs_par_defaut_sans_colonnes_seuil_et_delai():
    df = _frame().drop(columns=["seuil", "delai"])
    step = ReplenishmentPlanStep(date="semaine", by=["famille"], stock="stock", demand="ventes", forecast="prev",
                                 safety_weeks=1.0, lead_time=1)
    state = TransformState(df=df, prov={c: Provenance.REAL for c in df.columns})
    out = asyncio.run(apply_transforms(state, [step], ctx=None)).df
    futur = out[(out["famille"] == "A") & (out["semaine_plan"] >= 1)]
    assert set(futur["seuil_plan"]) == {30} and set(futur["delai_plan"]) == {1}


def test_colonne_stock_absente_est_une_erreur_claire():
    from apowerb.bi.spec.errors import SpecError

    with pytest.raises(SpecError, match="stock"):
        _run(_frame().drop(columns=["stock"]))

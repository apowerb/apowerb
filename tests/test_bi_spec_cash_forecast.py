"""Étape « cash_forecast » : solde réel projeté sur l'horizon en trois
scénarios (central / pessimiste / optimiste) et passage sous le seuil."""
import asyncio

import numpy as np
import pandas as pd
import pytest

from apowerb.bi.spec.errors import SpecError
from apowerb.bi.spec.models import CashForecastStep, Provenance
from apowerb.bi.spec.transforms import (
    CASH_COLUMNS,
    TransformState,
    apply_transforms,
    project_cash,
)


def _proj(balance0, inflow, outflow, *, a=0.1, b=0.1, seuil=0.0):
    return project_cash(float(balance0), np.asarray(inflow, dtype=float), np.asarray(outflow, dtype=float),
                        inflow_shock=a, outflow_shock=b, threshold=seuil)


# ---------------------------------------------------------------------------
# Règle de calcul (à la main)
# ---------------------------------------------------------------------------


def test_trois_scenarios_flux_positifs():
    # Solde 1000, encaissements 500, décaissements 400, a = b = 10 %.
    # central : +100 par période ; pessimiste : 450 - 440 = +10 ; optimiste : 550 - 360 = +190.
    p = _proj(1000, [500] * 4, [400] * 4)
    assert p["solde_projete_central"].tolist() == [1100, 1200, 1300, 1400]
    assert p["solde_projete_pessimiste"].tolist() == pytest.approx([1010, 1020, 1030, 1040])
    assert p["solde_projete_optimiste"].tolist() == pytest.approx([1190, 1380, 1570, 1760])
    assert p["encaissements_projetes"].tolist() == [500] * 4
    assert p["decaissements_projetes"].tolist() == [400] * 4
    assert p["flux_net_projete"].tolist() == [100] * 4


def test_scenarios_et_seuil_quand_la_tresorerie_baisse():
    # Solde 1000, encaissements 300, décaissements 400, seuil 700.
    # central -100 : 900, 800, 700, 600 ; pessimiste (270 / 440) -170 : 830, 660, 490, 320 ;
    # optimiste (330 / 360) -30 : 970, 940, 910, 880.
    p = _proj(1000, [300] * 4, [400] * 4, seuil=700)
    assert p["solde_projete_central"].tolist() == [900, 800, 700, 600]
    assert p["solde_projete_pessimiste"].tolist() == pytest.approx([830, 660, 490, 320])
    assert p["solde_projete_optimiste"].tolist() == pytest.approx([970, 940, 910, 880])
    # strictement sous le seuil : 700 n'est pas sous 700
    assert p["sous_seuil_central"].tolist() == [0, 0, 0, 1]
    assert p["sous_seuil_pessimiste"].tolist() == [0, 1, 1, 1]
    assert p["sous_seuil_optimiste"].tolist() == [0, 0, 0, 0]


def test_a_et_b_distincts_et_solde_negatif_non_plafonne():
    # a = 20 % sur les encaissements, b = 50 % sur les décaissements.
    p = _proj(100, [100] * 3, [100] * 3, a=0.2, b=0.5)
    assert p["solde_projete_central"].tolist() == [100, 100, 100]
    # pessimiste : 80 - 150 = -70 par période -> le solde devient négatif
    assert p["solde_projete_pessimiste"].tolist() == pytest.approx([30, -40, -110])
    # optimiste : 120 - 50 = +70
    assert p["solde_projete_optimiste"].tolist() == pytest.approx([170, 240, 310])


def test_flux_negatifs_ou_manquants_ramenes_a_zero():
    p = _proj(100, [np.nan, -50, 10], [20, 20, np.nan], a=0, b=0)
    assert p["encaissements_projetes"].tolist() == [0, 0, 10]
    assert p["decaissements_projetes"].tolist() == [20, 20, 0]
    assert p["solde_projete_central"].tolist() == [80, 60, 70]


# ---------------------------------------------------------------------------
# Étape complète
# ---------------------------------------------------------------------------

START = pd.Timestamp("2025-01-06")


def _frame(with_future=True, with_balance=True, comptes=("A", "B")):
    rows = []
    spec = {"A": (1000.0, 300.0, 400.0, 700.0), "B": (5000.0, 500.0, 400.0, 100.0)}
    for acc in comptes:
        solde, enc, dec, seuil = spec[acc]
        for w in range(20):
            row = {"semaine": START + pd.Timedelta(weeks=w), "compte": acc, "encaissements": enc,
                   "decaissements": dec, "solde": 2000.0 if w < 19 else solde, "seuil": seuil}
            if not with_balance:
                row.pop("solde")
            rows.append(row)
        if with_future:
            for w in range(20, 24):
                rows.append({"semaine": START + pd.Timedelta(weeks=w), "compte": acc,
                             "encaissements_prevus": enc, "decaissements_prevus": dec})
    return pd.DataFrame(rows)


def _run(df, **kw):
    kw.setdefault("balance", "solde")
    step = CashForecastStep(date="semaine", by=["compte"], horizon=4, **kw)
    state = TransformState(df=df, prov={c: Provenance.REAL for c in df.columns})
    return asyncio.run(apply_transforms(state, [step], ctx=None))


def test_etape_par_compte_avec_seuil_de_la_colonne():
    out = _run(_frame(), keep_history=3, min_cash_column="seuil").df
    a = out[out["compte"] == "A"].sort_values("semaine")
    assert len(a) == 3 + 4
    futur = a[a["semaine_plan"] >= 1]
    assert futur["semaine_plan"].tolist() == [1, 2, 3, 4]
    assert futur["solde_projete_central"].tolist() == [900, 800, 700, 600]
    assert futur["solde_projete_pessimiste"].tolist() == pytest.approx([830, 660, 490, 320])
    assert futur["sous_seuil_central"].tolist() == [0, 0, 0, 1]
    assert futur["sous_seuil_pessimiste"].tolist() == [0, 1, 1, 1]
    assert set(futur["seuil_tresorerie"]) == {700}
    actuel = a[a["semaine_plan"] == 0]
    assert len(actuel) == 1 and actuel["solde_projete_central"].iloc[0] == 1000
    b = out[(out["compte"] == "B") & (out["semaine_plan"] >= 1)]
    assert b["solde_projete_central"].tolist() == [5100, 5200, 5300, 5400]
    assert b["sous_seuil_pessimiste"].sum() == 0


def test_seuil_aussi_sur_l_historique_garde_avec_solde_reel_dans_les_trois_scenarios():
    # Leçon de #315 : la ligne de seuil couvre tout le graphique, pas seulement le futur.
    out = _run(_frame(), keep_history=3, min_cash_column="seuil").df
    a = out[(out["compte"] == "A") & ~(out["semaine_plan"] >= 1)].sort_values("semaine")
    assert len(a) == 3 and set(a["seuil_tresorerie"]) == {700}
    assert a["solde_projete_central"].tolist() == [2000, 2000, 1000]
    assert a["solde_projete_pessimiste"].tolist() == [2000, 2000, 1000]
    assert a["solde_projete_optimiste"].tolist() == [2000, 2000, 1000]
    assert a["semaine_plan"].tolist()[-1] == 0
    assert a["encaissements_projetes"].isna().all()
    # l'indicateur n'est posé que sur la dernière période réelle (1000 >= 700) et le futur
    assert a["sous_seuil_central"].tolist()[:2] == [pytest.approx(np.nan, nan_ok=True)] * 2
    assert a["sous_seuil_central"].iloc[-1] == 0


def test_solde_deja_sous_le_seuil_compte_comme_periode_zero():
    out = _run(_frame(), min_cash=1500.0).df
    a = out[(out["compte"] == "A") & (out["semaine_plan"] == 0)]
    assert a[["sous_seuil_central", "sous_seuil_pessimiste", "sous_seuil_optimiste"]].iloc[0].tolist() == [1, 1, 1]
    b = out[(out["compte"] == "B") & (out["semaine_plan"] == 0)]
    assert b["sous_seuil_central"].iloc[0] == 0


def test_seuil_parametre_et_historique_en_repli_sans_prevision():
    out = _run(_frame(with_future=False), keep_history=3, min_cash=1500.0).df
    a = out[(out["compte"] == "A") & ~(out["semaine_plan"] >= 1)]
    assert len(a) == 3 and set(a["seuil_tresorerie"]) == {1500}


def test_sans_prevision_repli_sur_la_moyenne_avec_avertissement():
    state = _run(_frame(with_future=False))
    a = state.df[(state.df["compte"] == "A") & (state.df["semaine_plan"] >= 1)].sort_values("semaine")
    assert len(a) == 4
    assert (a["encaissements_prevus"] == 300).all() and (a["decaissements_prevus"] == 400).all()
    assert a["semaine"].iloc[0] == START + pd.Timedelta(weeks=20)
    assert a["solde_projete_central"].tolist() == [900, 800, 700, 600]
    assert any("moyenne des 4 dernières périodes" in w for w in state.warnings)
    assert state.prov["encaissements_prevus"] == Provenance.FORECAST


def test_repli_sur_une_seule_colonne_et_fusion_des_lignes_de_deux_etapes_forecast():
    # Deux étapes forecast successives ajoutent chacune leurs lignes futures :
    # une ligne par date et par compte est reconstituée (pas de double comptage).
    df = _frame()
    fut_in = df[df["semaine"] > START + pd.Timedelta(weeks=19)].drop(columns=["decaissements_prevus"]).copy()
    fut_out = df[df["semaine"] > START + pd.Timedelta(weeks=19)].drop(columns=["encaissements_prevus"]).copy()
    base = df[df["semaine"] <= START + pd.Timedelta(weeks=19)]
    state = _run(pd.concat([base, fut_in, fut_out], ignore_index=True, sort=False))
    a = state.df[(state.df["compte"] == "A") & (state.df["semaine_plan"] >= 1)].sort_values("semaine")
    assert a["semaine"].tolist() == [START + pd.Timedelta(weeks=w) for w in range(20, 24)]
    assert a["solde_projete_central"].tolist() == [900, 800, 700, 600]
    assert not state.warnings
    # décaissements prévus absents : repli sur leur moyenne seulement pour ce flux
    state = _run(pd.concat([base, fut_in], ignore_index=True, sort=False))
    a = state.df[(state.df["compte"] == "A") & (state.df["semaine_plan"] >= 1)]
    assert (a["decaissements_projetes"] == 400).all() and (a["encaissements_projetes"] == 300).all()
    assert any("moyenne des 4 dernières périodes" in w for w in state.warnings)


def test_prevision_moteur_negative_ramenee_a_zero():
    df = _frame()
    df.loc[df["encaissements_prevus"].notna(), "encaissements_prevus"] = -10.0
    a = _run(df).df
    a = a[(a["compte"] == "A") & (a["semaine_plan"] >= 1)]
    assert (a["encaissements_projetes"] == 0).all()


def test_sans_colonne_solde_reconstitue_depuis_le_solde_d_ouverture():
    # Compte A : +300 - 400 = -100 par semaine sur 20 semaines, ouverture 5000 -> 3000.
    state = _run(_frame(with_balance=False), balance=None, opening_balance=5000.0)
    a = state.df[state.df["compte"] == "A"].sort_values("semaine")
    assert a[a["semaine_plan"] == 0]["solde_projete_central"].iloc[0] == pytest.approx(3000)
    futur = a[a["semaine_plan"] >= 1]
    assert futur["solde_projete_central"].tolist() == pytest.approx([2900, 2800, 2700, 2600])


def test_pas_de_groupe_et_chaine_mensuelle():
    rows = [{"mois": pd.Timestamp("2025-01-31") + pd.offsets.MonthEnd(m), "solde": 100.0 + m, "enc": 10.0,
             "dec": 5.0} for m in range(6)]
    df = pd.DataFrame(rows)
    step = CashForecastStep(date="mois", balance="solde", inflow="enc", outflow="dec", horizon=3, inflow_shock=0,
                            outflow_shock=0)
    state = TransformState(df=df, prov={c: Provenance.REAL for c in df.columns})
    out = asyncio.run(apply_transforms(state, [step], ctx=None)).df
    futur = out[out["semaine_plan"] >= 1].sort_values("mois")
    assert futur["mois"].tolist() == [pd.Timestamp("2025-07-31"), pd.Timestamp("2025-08-31"),
                                      pd.Timestamp("2025-09-30")]
    assert futur["solde_projete_central"].tolist() == [110, 115, 120]


def test_provenances_et_colonnes():
    state = _run(_frame(), min_cash_column="seuil")
    for col in CASH_COLUMNS:
        assert col in state.df.columns
    assert state.prov["solde_projete_central"] == Provenance.FORECAST
    assert state.prov["encaissements_projetes"] == Provenance.FORECAST
    assert state.prov["solde_projete_pessimiste"] == Provenance.SIMULATED
    assert state.prov["solde_projete_optimiste"] == Provenance.SIMULATED
    assert state.prov["seuil_tresorerie"] == Provenance.REAL
    assert state.prov["semaine_plan"] == Provenance.REAL
    assert state.prov["solde"] == Provenance.REAL
    assert _run(_frame()).prov["seuil_tresorerie"] == Provenance.SIMULATED


def test_entrees_non_modifiees():
    df = _frame()
    before = df.copy()
    _run(df)
    pd.testing.assert_frame_equal(df, before)


@pytest.mark.parametrize("drop", ["solde", "encaissements", "compte"])
def test_colonne_absente_est_une_erreur_claire(drop):
    with pytest.raises(SpecError, match=drop):
        _run(_frame().drop(columns=[drop]))


def test_validation_des_parametres_du_modele():
    with pytest.raises(ValueError):
        CashForecastStep(date="semaine", inflow_shock=1.5)
    with pytest.raises(ValueError):
        CashForecastStep(date="Semaine")

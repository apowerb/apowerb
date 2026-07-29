"""Tests du rattachement résiduel d'une ligne AR sans réf (CF101268).

Live 2026-06-02 (Andrez BRAJON / CAPRI) : AR scanné illisible -> ni l'intake
ni le recorder n'extraient les lignes -> elles sont recopiées depuis le PMI.
Le recorder copie ``Reference = lcctrefext`` ; or la ligne PMI codart
"19210323007" a ``lcctrefext`` VIDE (1/16, le reste = ``CAPRI ...``). La ligne
AR hérite d'une ``Reference`` vide -> inapariable -> faux ``LINE_NOT_IN_PO`` ->
faux ``non_rapproché``, alors que le code article existe bien au PO. Le
rattachement résiduel 1:1 par élimination le corrige.

Fonctions pures, pas de DB.
Run: .venv/bin/pytest tests/unit_scei/test_refless_line_residual_match.py
"""
from th2customers.scei.tools.scei_ar_persist import (
    _reconcile_lines,
    _derive_statut_global,
)

# 3 lignes PMI dont la 2e a lcctrefext VIDE (codart présent) — réplique CF101268.
PMI = [
    {"lcctcodart": "19210322003", "lcctrefext": "CAPRI 187204", "lcctqty": 200.0, "lcctpunet": 278.98, "lcctexpu": "C"},
    {"lcctcodart": "19210323007", "lcctrefext": "", "lcctqty": 50.0, "lcctpunet": 221.64, "lcctexpu": "C"},
    {"lcctcodart": "19210323008", "lcctrefext": "CAPRI 750834", "lcctqty": 40.0, "lcctpunet": 314.59, "lcctexpu": "C"},
]


def _ar():
    # Ce que le recorder LLM recopie depuis le PMI (Reference = lcctrefext).
    return [
        {"Reference": "CAPRI 187204", "Quantite": 200, "Prix": 278.98},
        {"Reference": "", "Quantite": 50, "Prix": 221.64},  # hérite du refext vide
        {"Reference": "CAPRI 750834", "Quantite": 40, "Prix": 314.59},
    ]


def test_refless_line_rescued_to_conforme():
    # Pas de pdf_text (scan illisible) : le résidu doit quand même rattacher.
    recon = _reconcile_lines(_ar(), PMI, pdf_text=None)
    assert len(recon) == 3
    # Rescue ET conforme (pas seulement "pas LINE_NOT_IN_PO") : qté AR == qté PMI.
    assert recon[1].get("TypeEcart") not in (
        "LINE_NOT_IN_PO", "ecart_qte", "ecart_prix", "ecart_date"
    )
    assert _derive_statut_global("matched", recon) == "conforme"


def test_parasite_empty_line_without_qty_not_rescued():
    # Ligne parasite vide SANS quantité : la garde contenu bloque le rescue
    # -> reste non_rapproché (pas de faux conforme). Réserve reviewer 2026-06-02.
    ar = _ar()
    ar[1] = {"Reference": "", "Prix": 221.64}  # aucune quantité
    recon = _reconcile_lines(ar, PMI, pdf_text=None)
    assert recon[1]["TypeEcart"] == "LINE_NOT_IN_PO"
    assert _derive_statut_global("matched", recon) == "non_rapproche"


def test_parasite_empty_line_wrong_qty_not_rescued():
    # Ligne vide avec quantité incohérente (99 vs PMI 50) -> pas de rescue.
    ar = _ar()
    ar[1] = {"Reference": "", "Quantite": 99, "Prix": 221.64}
    recon = _reconcile_lines(ar, PMI, pdf_text=None)
    assert recon[1]["TypeEcart"] == "LINE_NOT_IN_PO"
    assert _derive_statut_global("matched", recon) == "non_rapproche"


def test_real_unknown_line_still_line_not_in_po():
    # Ligne AR portant une vraie réf absente du PO -> NON rattachée (vrai
    # LINE_NOT_IN_PO) : le résidu ne touche que les lignes SANS aucune réf.
    ar = _ar()
    ar[1] = {"Reference": "ZZZ-INCONNU-999", "Quantite": 50, "Prix": 221.64}
    recon = _reconcile_lines(ar, PMI, pdf_text=None)
    assert recon[1]["TypeEcart"] == "LINE_NOT_IN_PO"
    assert _derive_statut_global("matched", recon) == "non_rapproche"


def test_two_empty_pmi_refext_is_ambiguous_no_rescue():
    # 2 lignes PMI à refext vide -> ambigu -> on ne rattache rien (conservateur).
    pmi = [
        {"lcctcodart": "19210322003", "lcctrefext": "CAPRI 187204", "lcctqty": 200.0, "lcctpunet": 278.98, "lcctexpu": "C"},
        {"lcctcodart": "19210323007", "lcctrefext": "", "lcctqty": 50.0, "lcctpunet": 221.64, "lcctexpu": "C"},
        {"lcctcodart": "19210323008", "lcctrefext": "", "lcctqty": 40.0, "lcctpunet": 314.59, "lcctexpu": "C"},
    ]
    ar = [
        {"Reference": "CAPRI 187204", "Quantite": 200, "Prix": 278.98},
        {"Reference": "", "Quantite": 50, "Prix": 221.64},
        {"Reference": "", "Quantite": 40, "Prix": 314.59},
    ]
    recon = _reconcile_lines(ar, pmi, pdf_text=None)
    # Ambiguïté -> statut non_rapproché (au moins une ligne reste LINE_NOT_IN_PO).
    assert _derive_statut_global("matched", recon) == "non_rapproche"

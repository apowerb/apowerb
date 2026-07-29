"""Tests du PDF-bridge Customer Item (DOVITECH).

Live 2026-06-01 CF101308/309/311 (DOVITECH) : l'AR ne porte que le numero
fournisseur "D0160R30N01-01" tandis que PMI cle sur le Customer Item
"570999860001" (codart) ; les deux sont imprimes sur la meme ligne de
l'Order confirmation. Le matcher principal echoue (aucune ref ne contient le
codart) -> faux LINE_NOT_IN_PO -> non_rapproche. Le PDF-bridge rattrape.

Fonctions pures, pas de DB.
Run: .venv/bin/pytest tests/unit_scei/test_pdf_bridge_customer_item.py
"""
from th2customers.scei.tools.scei_ar_persist import (
    _reconcile_lines,
    _derive_statut_global,
    _bridge_unmatched_via_pdf,
    _match_lines_to_pmi,
)

PMI_DOVITECH = [{"lcctcodart": "570999860001", "lcctrefext": "", "lcctqty": 140.0}]
# Texte type extrait par fitz : ref fournisseur et Customer Item cote a cote.
PDF_OK = (
    "Order confirmation\n"
    "Pos. Item number Customer Item Description Quantity\n"
    "1 D0160R30N01-01 570999860001 Nanocrystalline core 160x130x30mm 140 Stk\n"
)


def _ar():
    return [{"Reference": "D0160R30N01-01", "QuantiteAR": 140}]


def test_bridge_rescues_dovitech_to_conforme():
    recon = _reconcile_lines(_ar(), PMI_DOVITECH, pdf_text=PDF_OK)
    assert len(recon) == 1
    assert recon[0]["TypeEcart"] != "LINE_NOT_IN_PO"
    assert recon[0]["Situation"] == "OK"
    assert _derive_statut_global("matched", recon) == "conforme"


def test_no_pdf_text_stays_line_not_in_po():
    recon = _reconcile_lines(_ar(), PMI_DOVITECH, pdf_text=None)
    assert recon[0]["TypeEcart"] == "LINE_NOT_IN_PO"
    assert _derive_statut_global("matched", recon) == "non_rapproche"


def test_codart_absent_from_pdf_no_bridge():
    pdf = "Order confirmation\n1 D0160R30N01-01 Nanocrystalline core 140 Stk\n"
    recon = _reconcile_lines(_ar(), PMI_DOVITECH, pdf_text=pdf)
    assert recon[0]["TypeEcart"] == "LINE_NOT_IN_PO"


def test_codart_far_from_ref_no_bridge():
    # codart present mais a >120 chars de la ref fournisseur -> pas le meme row.
    pdf = (
        "1 D0160R30N01-01 some long description "
        + ("x" * 200)
        + " 570999860001 autre contexte\n"
    )
    recon = _reconcile_lines(_ar(), PMI_DOVITECH, pdf_text=pdf)
    assert recon[0]["TypeEcart"] == "LINE_NOT_IN_PO"


def test_ambiguous_two_codarts_near_ref_no_bridge():
    pmi = [
        {"lcctcodart": "570999860001", "lcctrefext": "", "lcctqty": 140.0},
        {"lcctcodart": "570999860002", "lcctrefext": "", "lcctqty": 140.0},
    ]
    pdf = "1 D0160R30N01-01 570999860001 570999860002 140 Stk\n"
    ar_refs = [["D0160R30N01-01"]]
    matches = _match_lines_to_pmi(ar_refs, pmi)
    matches = _bridge_unmatched_via_pdf(ar_refs, pmi, matches, pdf)
    assert matches[0] is None  # 2 candidats -> conservateur -> pas de pont


def test_codart_beyond_tight_window_no_bridge():
    # Le codart est present mais a ~90 chars de la ref fournisseur (cas d'une
    # ligne PMI VOISINE dans un PDF dense). Avec la fenetre serree (60), aucun
    # pont -> reste LINE_NOT_IN_PO (conservateur, pas de faux conforme).
    pdf = (
        "1 D0160R30N01-01 Nanocrystalline core some long description here padding "
        "xxxxxxxxxxxxxxx 570999860001 140 Stk\n"
    )
    recon = _reconcile_lines(_ar(), PMI_DOVITECH, pdf_text=pdf)
    assert recon[0]["TypeEcart"] == "LINE_NOT_IN_PO"


def test_multiline_dense_no_proximity_bridge():
    # RELLY-like : 2 lignes AR non matchees, codarts proches dans le PDF (layout
    # dense ou le codart precede la ref -> le codart voisin est plus proche). La
    # passe proximite NE DOIT PAS ponter quand >1 ligne est non matchee (eviter le
    # mauvais appariement decale d'une ligne). Multi-ligne customer-item -> ref_client.
    pmi = [
        {"lcctcodart": "574092110001", "lcctrefext": "", "lcctqty": 20.0},
        {"lcctcodart": "574092120001", "lcctrefext": "", "lcctqty": 20.0},
    ]
    pdf = "574092110001 PANNEAU\n7MOLL1179\n574092120001 PANNEAU\n7MOLL1180\n"
    ar_refs = [["7MOLL1179"], ["7MOLL1180"]]
    matches = _match_lines_to_pmi(ar_refs, pmi)
    assert matches == [None, None]
    matches = _bridge_unmatched_via_pdf(ar_refs, pmi, matches, pdf)
    assert matches == [None, None], f"multi-ligne ne doit pas ponter par proximite ; got {matches}"

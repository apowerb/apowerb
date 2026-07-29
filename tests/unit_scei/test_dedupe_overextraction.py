"""Tests for _dedupe_overextracted_lines + its effect on the StatutGlobal.

Incident 2026-05-28 CF097349 (Danfoss): the intake split one PO line into 4
same-ref rows; the 1:1 guard then flagged all of them LINE_NOT_IN_PO -> faux
non_rapproché. Dedup must collapse them and reconcile -> conforme.

Pure functions, no DB. Run: .venv/bin/pytest tests/unit_scei/test_dedupe_overextraction.py
"""
from th2customers.scei.tools.scei_ar_persist import (
    _reconcile_lines,
    _derive_statut_global,
    _dedupe_overextracted_lines,
)

PMI_DANFOSS = [{"lcctcodart": "13651670001", "lcctrefext": "100076-1", "lcctqty": 30.0}]


def _ar_line(ref, qty, prix=129.36):
    return {"Reference": ref, "QuantiteAR": qty, "PrixAR": prix, "PrixERP": prix}


def test_danfoss_overextraction_collapses_to_conforme():
    # 1 article (100076-1) split by the intake into 4 rows (30/2/28/2).
    lignes = [_ar_line("100076-1", q) for q in (30, 2, 28, 2)]
    recon = _reconcile_lines(lignes, PMI_DANFOSS)
    assert len(recon) == 1, f"expected 1 line after dedup, got {len(recon)}"
    assert recon[0]["TypeEcart"] is None
    assert recon[0]["Situation"] == "OK"
    assert _derive_statut_global("matched", recon) == "conforme"


def test_dedupe_keeps_line_matching_pmi_qty():
    # The kept representative is the one whose qty matches PMI (30), not the first.
    lignes = [_ar_line("100076-1", 2), _ar_line("100076-1", 30), _ar_line("100076-1", 28)]
    kept = _dedupe_overextracted_lines(lignes, PMI_DANFOSS)
    assert len(kept) == 1
    assert kept[0]["QuantiteAR"] == 30


def test_legit_multiline_distinct_refs_unchanged():
    pmi = [
        {"lcctcodart": "13651670001", "lcctrefext": "100076-1", "lcctqty": 30.0},
        {"lcctcodart": "99999999999", "lcctrefext": "200055-7", "lcctqty": 10.0},
    ]
    lignes = [_ar_line("100076-1", 30), _ar_line("200055-7", 10)]
    recon = _reconcile_lines(lignes, pmi)
    assert len(recon) == 2  # distinct refs -> no dedup
    assert all(l["Situation"] == "OK" and l["TypeEcart"] is None for l in recon)
    assert _derive_statut_global("matched", recon) == "conforme"


def test_dup_ref_not_in_pmi_is_kept_as_line_not_in_po():
    # Same ref repeated but absent from PMI -> NOT a resolvable over-extraction:
    # keep all, genuine LINE_NOT_IN_PO -> non_rapproche.
    lignes = [_ar_line("ZZZ-404", 5), _ar_line("ZZZ-404", 7)]
    recon = _reconcile_lines(lignes, PMI_DANFOSS)
    assert len(recon) == 2
    assert all(l["TypeEcart"] == "LINE_NOT_IN_PO" for l in recon)
    assert _derive_statut_global("matched", recon) == "non_rapproche"


def test_dup_ref_no_qty_match_keeps_first_then_ecart_qte():
    # PMI qty 30; AR dup lines 10 & 15 (a genuine split summing to 25, or noise).
    # David's rule = keep the line matching PMI; none matches -> keep first (10)
    # -> ecart_qte -> non_conforme (honest "can't cleanly reconcile").
    lignes = [_ar_line("100076-1", 10), _ar_line("100076-1", 15)]
    recon = _reconcile_lines(lignes, PMI_DANFOSS)
    assert len(recon) == 1
    assert recon[0]["QuantiteAR"] == 10
    assert recon[0]["TypeEcart"] == "ecart_qte"
    assert _derive_statut_global("matched", recon) == "non_conforme"


def test_distinct_raw_refs_same_norm_not_collapsed():
    # Two genuinely distinct raw refs that normalize identically ("100076-1" vs
    # "100076.1" -> "1000761") must NOT be silently collapsed (conservative
    # guard, reviewer finding): keep both lines.
    lignes = [_ar_line("100076-1", 30), _ar_line("100076.1", 5)]
    kept = _dedupe_overextracted_lines(lignes, PMI_DANFOSS)
    assert len(kept) == 2


def test_no_pmi_lines_is_noop():
    lignes = [_ar_line("100076-1", 30), _ar_line("100076-1", 2)]
    assert _dedupe_overextracted_lines(lignes, []) == lignes
    assert _reconcile_lines(lignes, []) == lignes

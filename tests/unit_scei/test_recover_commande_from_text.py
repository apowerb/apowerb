"""Deterministic recovery of the CF order number from intake_pdf_text.

Live false negative 2026-05-27 (CF101159): when the intake LLM fails to produce
a valid commande_number_sql, the matcher gate falls back to the LLM, which
queries with the wrong format (CF101159 instead of 101159) -> PO not found
-> false non_rapproche. The number IS present in intake_pdf_text (V2 gate). This
helper recovers it deterministically, CONSERVATIVELY: only when exactly one
unambiguous CF<6digits> appears (else None -> no false positive).
"""
from __future__ import annotations

from th2customers.scei.superagents.scei_pmi_match import _recover_commande_from_text


def test_single_cf_recovered():
    assert _recover_commande_from_text("RE: CF101159 // ARC ... Vos Ref CF101159") == "101159"

def test_cf_with_space():
    assert _recover_commande_from_text("commande CF 101159 du jour") == "101159"

def test_leading_zeros():
    assert _recover_commande_from_text("PO CF005493 confirmed") == "005493"

def test_two_distinct_cf_is_ambiguous_none():
    assert _recover_commande_from_text("CF101159 et CF100688 sur le meme doc") is None

def test_no_cf_none():
    assert _recover_commande_from_text("Sales Order 679424 NOVAPLEST") is None

def test_empty_none():
    assert _recover_commande_from_text("") is None
    assert _recover_commande_from_text(None) is None

def test_same_number_repeated_is_single():
    assert _recover_commande_from_text("CF101159 ... encore CF101159 plus bas") == "101159"

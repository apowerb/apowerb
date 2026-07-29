"""FIX 1 — TDD : normalisation deterministe du n de commande dans le gate matcher.

Incident 2026-05-23 : un n composite type CF100688/124212881 ou 100688/124212881
arrive dans commande_number_sql et le gate rejette en out_of_scope SANS interroger PMI.

Comportement attendu (deterministe, AVANT le rejet) :
- num_sql non valide (pas ^\\d{6}$) -> tenter de deriver 6 chiffres :
  1. motif CF\\s*0*(\\d{6}) dans commande_number_sql puis commande_number_display
  2. sinon un unique groupe de 6 chiffres isole dans num_sql
  3. sinon out_of_scope (pas de faux match)
- num_sql deja valide (6 chiffres) : inchange.
"""
from __future__ import annotations


def _make_sql_match(ecktnumero: str):
    """run_sql qui matche en step A uniquement le ECKTNUMERO attendu."""
    def run_sql(sql: str) -> dict:
        up = sql.strip().upper()
        if "ECOMFOU" in up and "LCOMFOU" not in up:
            if "ECKTNUMERO=" + chr(39) + ecktnumero + chr(39) in sql.replace(" ", ""):
                return {
                    "success": True,
                    "data": [{
                        "ECKTSOC": "SC", "ECKTNUMERO": ecktnumero, "ECKTINDICE": "A",
                        "ECCTCODE": "S", "ECCTNOM": "ACME", "ECCTREFCDE": "R",
                    }],
                }
            return {"success": True, "data": []}
        return {"success": True, "data": []}
    return run_sql


def _intake(num_sql, display=None):
    extraction = {"commande_number_sql": num_sql, "lines": []}
    if display is not None:
        extraction["commande_number_display"] = display
    return {"email_classification": "ar", "extraction_results": extraction}


def test_composite_with_cf_prefix_normalizes_and_matches():
    from th2customers.scei.superagents.scei_pmi_match import match_ar_to_pmi
    intake = _intake("CF100688/124212881", display="CF100688/124212881")
    res = match_ar_to_pmi(intake, _make_sql_match("100688"))
    assert res["status"] != "out_of_scope"
    assert "100688" in res["diagnostic"]


def test_composite_without_prefix_uses_isolated_six_digits():
    from th2customers.scei.superagents.scei_pmi_match import match_ar_to_pmi
    # CF deja strippe mais composite garde : 100688 isole + 124212881 (9 chiffres, pas 6)
    intake = _intake("100688/124212881")
    res = match_ar_to_pmi(intake, _make_sql_match("100688"))
    assert res["status"] != "out_of_scope"
    assert "100688" in res["diagnostic"]


def test_cf_prefix_alone_normalizes():
    from th2customers.scei.superagents.scei_pmi_match import match_ar_to_pmi
    intake = _intake("CF101087", display="CF101087")
    res = match_ar_to_pmi(intake, _make_sql_match("101087"))
    assert res["status"] != "out_of_scope"
    assert "101087" in res["diagnostic"]


def test_valid_six_digits_unchanged():
    from th2customers.scei.superagents.scei_pmi_match import match_ar_to_pmi
    intake = _intake("100688")
    res = match_ar_to_pmi(intake, _make_sql_match("100688"))
    assert res["status"] != "out_of_scope"
    assert "100688" in res["diagnostic"]


def test_short_ambiguous_cf09xx_stays_out_of_scope():
    from th2customers.scei.superagents.scei_pmi_match import match_ar_to_pmi
    intake = _intake("CF0916", display="CF0916")
    res = match_ar_to_pmi(intake, _make_sql_match("999999"))
    assert res["status"] == "out_of_scope"


def test_empty_stays_out_of_scope():
    from th2customers.scei.superagents.scei_pmi_match import match_ar_to_pmi
    intake = _intake("")
    res = match_ar_to_pmi(intake, _make_sql_match("999999"))
    assert res["status"] == "out_of_scope"


def test_multiple_six_digit_groups_no_cf_is_ambiguous_out_of_scope():
    from th2customers.scei.superagents.scei_pmi_match import match_ar_to_pmi
    # deux groupes de 6 chiffres isoles, aucun prefixe CF -> ambigu -> out_of_scope
    intake = _intake("100688 234567")
    res = match_ar_to_pmi(intake, _make_sql_match("100688"))
    assert res["status"] == "out_of_scope"


# --- CORRECTION 1 — garde de fin anti faux-match (regex CF) ---------------
# Sans ancrage de fin, CF12345678 (8 chiffres) tronquait SILENCIEUSEMENT en
# 123456 -> risque de rapprochement sur un MAUVAIS bon de commande.

def test_cf_eight_digits_no_silent_truncation():
    from th2customers.scei.superagents.scei_pmi_match import _normalize_commande_number
    assert _normalize_commande_number("CF12345678", "CF12345678") is None


def test_cf_seven_digits_no_silent_truncation():
    from th2customers.scei.superagents.scei_pmi_match import _normalize_commande_number
    assert _normalize_commande_number("CF1234567", "CF1234567") is None


def test_cf_leading_zeros_not_broken_by_end_guard():
    from th2customers.scei.superagents.scei_pmi_match import _normalize_commande_number
    # incident 23/05 : CF005493 / CF0005493 (cas PMI reel) doivent rester valides
    assert _normalize_commande_number("CF005493", "CF005493") == "005493"
    assert _normalize_commande_number("CF0005493", "CF0005493") == "005493"


def test_cf_composite_with_slash_still_matches():
    from th2customers.scei.superagents.scei_pmi_match import _normalize_commande_number
    # le / apres les 6 chiffres n est pas un chiffre -> match OK
    assert _normalize_commande_number("CF100688/124212881", "CF100688/124212881") == "100688"


def test_bare_nine_digits_out_of_scope():
    from th2customers.scei.superagents.scei_pmi_match import _normalize_commande_number
    assert _normalize_commande_number("124212881", "124212881") is None


def test_supplier_format_out_of_scope():
    from th2customers.scei.superagents.scei_pmi_match import _normalize_commande_number
    assert _normalize_commande_number("30028499-3834", "30028499-3834") is None

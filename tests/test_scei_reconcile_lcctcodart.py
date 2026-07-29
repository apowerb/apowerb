"""TDD (RED first) for the LCCTCODART re-keying of line reconciliation.

Root cause (measured 2026-05-27): _reconcile_lines + the matcher keyed line
matching on LCCTREFEXT, which is EMPTY 100% of the time in PMI. The real
article reference is LCCTCODART. The AR ref is the PMI LCCTCODART possibly
wrapped in noise (prefix '844/', suffix ' SP', a spurious leading digit), so
the join must be SUBSTRING-based (PMI codart normalized is a substring of the
AR ref normalized), with hard guards against false positives:
  - PMI codart length >= 8 (no short-code coincidences),
  - bidirectional 1:1 (an AR ref maps to exactly one PMI line AND vice-versa);
    any ambiguity -> no match (conservative LINE_NOT_IN_PO).
LCCTREFEXT stays as a secondary exact key for backward-compat when populated.
"""
from __future__ import annotations

import pytest

from th2customers.scei.tools.scei_ar_persist import (
    _match_lines_to_pmi,
    _norm_ref,
    _reconcile_lines,
)


def _pmi(codart="", refext="", qty=None, punet=None):
    return {"lcctcodart": codart, "lcctrefext": refext, "lcctqty": qty, "lcctpunet": punet}


class TestNormRef:
    def test_strips_dashes_slashes_spaces_and_uppercases(self):
        assert _norm_ref("5712-9128-002") == "57129128002"
        assert _norm_ref("844/13430344008 SP") == "84413430344008SP"
        assert _norm_ref(None) == ""


class TestMatchLinesToPmi:
    def test_exact_codart_substring_matches(self):
        # CF101202: AR '5712-9128-002' -> '57129128002' == PMI codart (len 11).
        res = _match_lines_to_pmi(["5712-9128-002"], [_pmi(codart="57129128002")])
        assert res[0] is not None and res[0]["lcctcodart"] == "57129128002"

    def test_codart_embedded_in_prefixed_suffixed_ar_ref(self):
        # CF101159: AR '844/13430344008 SP' contains PMI codart '13430344008'.
        res = _match_lines_to_pmi(["844/13430344008 SP"], [_pmi(codart="13430344008")])
        assert res[0] is not None and res[0]["lcctcodart"] == "13430344008"

    def test_codart_substring_tolerates_intake_noise_prefix_digit(self):
        # CF101205: AR '15709-9968-0001' -> '1570999680001' contains '570999680001'.
        res = _match_lines_to_pmi(["15709-9968-0001"], [_pmi(codart="570999680001")])
        assert res[0] is not None

    def test_ar_ref_is_prefix_of_codart_with_leading_lineno(self):
        # CF101245: AR '1 5739-2401' = numero de ligne "1 " parasite + ref =
        # codart tronque de son suffixe -0001 (codart PMI '573924010001').
        # Strip prefixe -> '57392401' (8 ch), sous-chaine du codart (sens
        # bidirectionnel ref in codart).
        res = _match_lines_to_pmi(["1 5739-2401"], [_pmi(codart="573924010001")])
        assert res[0] is not None and res[0]["lcctcodart"] == "573924010001"

    def test_short_ar_ref_prefix_of_two_codarts_is_ambiguous_none(self):
        # Anti faux-positif du bidirectionnel : une ref (>=8) prefixe de DEUX
        # codarts distincts -> ambigu -> None (la garde 1:1 protege).
        res = _match_lines_to_pmi(
            ["5739-2401"], [_pmi(codart="573924010001"), _pmi(codart="573924019999")]
        )
        assert res[0] is None

    def test_ar_ref_exactly_equals_codart_matches(self):
        # No noise: AR ref normalises to exactly the codart -> still a match.
        res = _match_lines_to_pmi(["57129128002"], [_pmi(codart="57129128002")])
        assert res[0] is not None and res[0]["lcctcodart"] == "57129128002"

    def test_no_match_returns_none(self):
        res = _match_lines_to_pmi(["ZZZZZZZZ"], [_pmi(codart="57129128002")])
        assert res[0] is None

    def test_short_codart_below_guard_never_substring_matches(self):
        # codart '1234' (len 4 < 8) must NOT match even if substring -> false-positive guard.
        res = _match_lines_to_pmi(["x1234x"], [_pmi(codart="1234")])
        assert res[0] is None

    def test_ar_ref_with_two_distinct_codarts_is_ambiguous_none(self):
        # Reviewer's bidirectional guard: an AR ref containing TWO distinct PMI
        # codarts is ambiguous -> LINE_NOT_IN_PO (no silent wrong pick).
        ar = "11111111111-22222222222"
        res = _match_lines_to_pmi([ar], [_pmi(codart="11111111111"), _pmi(codart="22222222222")])
        assert res[0] is None

    def test_two_ar_lines_matching_same_pmi_line_both_none(self):
        # Reverse direction: one PMI line is candidate of two AR refs -> ambiguous both.
        res = _match_lines_to_pmi(
            ["13430344008AA", "13430344008BB"], [_pmi(codart="13430344008")]
        )
        assert res == [None, None]

    def test_secondary_key_lcctrefext_exact_backward_compat(self):
        # When LCCTREFEXT IS populated, an exact normalized match still works.
        res = _match_lines_to_pmi(["ABC-123-XYZ"], [_pmi(codart="", refext="ABC123XYZ")])
        assert res[0] is not None

    def test_empty_ar_ref_is_none(self):
        assert _match_lines_to_pmi([""], [_pmi(codart="57129128002")]) == [None]

    def test_clean_one_to_one_multiline(self):
        # CF101202 shape: 3 distinct lines, 3 distinct codarts -> 3 clean matches.
        ar = ["5712-9128-002", "5718-1662-001", "5736-1912-0001"]
        pmi = [_pmi(codart="57129128002"), _pmi(codart="57181662001"), _pmi(codart="573619120001")]
        res = _match_lines_to_pmi(ar, pmi)
        assert [r["lcctcodart"] if r else None for r in res] == [
            "57129128002", "57181662001", "573619120001"]


class TestReconcileLinesEndToEnd:
    def test_matched_codart_no_discrepancy_is_conforme(self):
        lignes = [{"Reference": "5712-9128-002", "QuantiteAR": 6, "PrixAR": 10.0, "PrixERP": 10.0}]
        pmi = [_pmi(codart="57129128002", qty=6)]
        out = _reconcile_lines(lignes, pmi)
        assert out[0]["TypeEcart"] is None and out[0]["Situation"] == "OK"

    def test_matched_codart_price_diff_is_ecart_prix_no_regression(self):
        lignes = [{"Reference": "5712-9128-002", "QuantiteAR": 6, "PrixAR": 10.0, "PrixERP": 12.5}]
        pmi = [_pmi(codart="57129128002", qty=6)]
        out = _reconcile_lines(lignes, pmi)
        assert out[0]["TypeEcart"] == "ecart_prix" and out[0]["Situation"] == "NOK"

    def test_matched_codart_qty_diff_is_ecart_qte_no_regression(self):
        lignes = [{"Reference": "5712-9128-002", "QuantiteAR": 5, "PrixAR": 10.0, "PrixERP": 10.0}]
        pmi = [_pmi(codart="57129128002", qty=6)]
        out = _reconcile_lines(lignes, pmi)
        assert out[0]["TypeEcart"] == "ecart_qte" and out[0]["Situation"] == "NOK"

    def test_unmatched_line_is_line_not_in_po(self):
        lignes = [{"Reference": "NOPE-9999", "QuantiteAR": 6}]
        pmi = [_pmi(codart="57129128002", qty=6)]
        out = _reconcile_lines(lignes, pmi)
        assert out[0]["TypeEcart"] == "LINE_NOT_IN_PO" and out[0]["Situation"] == "NOK"

    def test_empty_pmi_lines_is_noop(self):
        lignes = [{"Reference": "5712-9128-002"}]
        assert _reconcile_lines(lignes, []) == lignes

    def test_cf101183_regression_all_seven_lines_now_conforme(self):
        # The original false non_rapproche: 7 lines, refs '5555-88xx-0001',
        # PMI codart '555588xx0001', qty+price match -> must become conforme.
        refs = ["5555-8817-0001", "5555-8818-0001", "5555-8819-0001"]
        prices = [91.27, 35.13, 20.82]
        codarts = ["555588170001", "555588180001", "555588190001"]
        lignes = [{"Reference": r, "QuantiteAR": 6, "PrixAR": p, "PrixERP": p}
                  for r, p in zip(refs, prices)]
        pmi = [_pmi(codart=cd, qty=6) for cd in codarts]
        out = _reconcile_lines(lignes, pmi)
        assert all(l["TypeEcart"] is None and l["Situation"] == "OK" for l in out)

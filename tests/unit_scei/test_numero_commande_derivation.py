"""Deterministic NumeroCommande derivation in tool_persist_ar_record.

Root cause (cleanup 2026-05-27): the recorder LLM provides NumeroCommande as a
free field and drifts — it writes the supplier ref ("1609427486", "3BS619638")
or a composite ("CF100688/124212881") instead of the matched PMI order number.
Like StatutGlobal, NumeroCommande must be DERIVED from ar_match.po.ecktnumero
(the deterministic matcher output) and OVERRIDE the LLM value. When the AR is
not matched (no po), we keep the LLM value as a fallback.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from th2customers.scei.tools import scei_ar_persist as P
from th2customers.scei.tools.scei_ar_persist import _derive_numero_commande


def _m(ecktnumero, status="matched"):
    return {"status": status, "po": {"ecktsoc": "100", "ecktnumero": ecktnumero}}


# --- helper pur --------------------------------------------------------------

def test_matched_overrides_composite():
    assert _derive_numero_commande(_m("100688"), "CF100688/124212881") == "CF100688"

def test_matched_overrides_supplier_ref():
    assert _derive_numero_commande(_m("101091"), "1609427486") == "CF101091"

def test_matched_preserves_leading_zeros():
    assert _derive_numero_commande(_m("005493"), "CF005493") == "CF005493"

def test_matched_strips_whitespace():
    assert _derive_numero_commande(_m("100688 "), "x") == "CF100688"

def test_already_cf_prefixed_not_doubled():
    assert _derive_numero_commande(_m("CF100688"), "x") == "CF100688"

def test_not_matched_keeps_llm_value():
    assert _derive_numero_commande({"status": "out_of_scope", "po": None}, "CF101043") == "CF101043"

def test_non_rapproche_keeps_llm_value():
    assert _derive_numero_commande({"status": "non_rapproche"}, "CF999999") == "CF999999"

def test_none_ar_match_keeps_llm_value():
    assert _derive_numero_commande(None, "CF123456") == "CF123456"

def test_matched_but_empty_ecktnumero_keeps_llm_value():
    assert _derive_numero_commande(_m(""), "CF777777") == "CF777777"

def test_matched_but_po_missing_keeps_llm_value():
    assert _derive_numero_commande({"status": "matched"}, "CF555555") == "CF555555"

# --- guard 6 chiffres (reserve critique 2026-05-27) --------------------------

def test_matched_internal_space_keeps_llm_value():
    assert _derive_numero_commande(_m("100 68"), "CF777777") == "CF777777"

def test_matched_non_six_digit_keeps_llm_value():
    assert _derive_numero_commande(_m("12345"), "CF777777") == "CF777777"

def test_matched_composite_in_ecktnumero_keeps_llm_value():
    assert _derive_numero_commande(_m("100688/124212881"), "CF000000") == "CF000000"


# --- wiring dans tool_persist_ar_record (override effectif) ------------------

def _capture_insert_params(ctx, llm_numero):
    captured = {}
    class _StubConn:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def execute(self, _sql, params=None):
            if params: captured.update(params)
            class _R:
                def scalar_one(_s): return 1
            return _R()
        def commit(self): pass
    class _StubEngine:
        def connect(self): return _StubConn()
        def begin(self): return _StubConn()
    orig = P._get_db_engine
    P._get_db_engine = lambda: _StubEngine()
    try:
        P.tool_persist_ar_record(
            NumeroCommande=llm_numero,
            DateCommande="2026-05-20",
            StatutGlobal="conforme",
            Societe="100",
            tool_context=ctx,
        )
    finally:
        P._get_db_engine = orig
    return captured

def test_wiring_override_on_matched():
    ctx = MagicMock()
    ctx.state = {"webhook_log_id": 1, "ar_match": {"status": "matched", "po": {"ecktnumero": "100688"}}}
    p = _capture_insert_params(ctx, "CF100688/124212881")
    assert p.get("NumeroCommande") == "CF100688"

def test_wiring_fallback_on_not_matched():
    ctx = MagicMock()
    ctx.state = {"webhook_log_id": 1, "ar_match": {"status": "out_of_scope", "po": None}}
    p = _capture_insert_params(ctx, "CF099999")
    assert p.get("NumeroCommande") == "CF099999"

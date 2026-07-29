"""pmi_match_gate: deterministic recovery from intake_pdf_text.

Live false negative 2026-05-27 (CF101159): intake failed -> gate bailed to the
LLM matcher -> queried raw \x27CF101159\x27 unnormalised -> PO not found -> faux
non_rapproche. The gate now recovers the CF number from intake_pdf_text and runs
the deterministic match (normalised \x27101159\x27) instead of bailing.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch


class FakePart:
    def __init__(self, text): self.text = text

class FakeContent:
    def __init__(self, role, parts): self.role = role; self.parts = parts


def _ctx(state):
    c = MagicMock(); c.state = state; c.agent_name = "scei_ar_matcher"; return c


def _run_sql_capture(captured):
    def run_sql(sql):
        captured.append(sql)
        flat = sql.replace(" ", "")
        if "ECOMFOU" in sql and "101159" in sql:
            return {"success": True, "data": [{
                "ECKTSOC": "100", "ECKTNUMERO": "101159", "ECKTINDICE": "0",
                "ECCTCODE": "100", "ECCTNOM": "NOVAPLEST", "ECCTREFCDE": "",
            }]}
        return {"success": True, "data": []}
    run_sql.__name__ = "tool_run_sql"
    return run_sql


def _call_gate(state):
    from th2customers.scei.gates import build_pmi_match_gate_callback
    captured = []
    cb = build_pmi_match_gate_callback(output_key="ar_match", tool_config_id="14", owner_id="com@scei88.fr")
    with patch("th2customers.scei.gates.load_tool_config_params",
               return_value=("database", {"DB_TYPE": "mssql", "DB_NAME": "PMI"})), \
         patch("th2customers.scei.gates.make_database_tools",
               return_value=[_run_sql_capture(captured)]), \
         patch("google.genai.types.Content", FakeContent), \
         patch("google.genai.types.Part", FakePart):
        result = cb(_ctx(state))
    return result, captured


def test_error_intake_recovers_number_and_matches_normalised():
    state = {"ar_intake": json.dumps({"__error__": "extract_failed"}),
             "intake_pdf_text": "RE: CF101159 // ARC NOVAPLEST Vos Ref CF101159"}
    result, captured = _call_gate(state)
    assert result is not None, "must NOT bail to the LLM"
    assert "ar_match" in state
    assert json.loads(state["ar_match"]).get("status") == "matched"
    assert any("ECKTNUMERO=\x27101159\x27" in s.replace(" ", "") for s in captured), captured
    assert not any("CF101159" in s for s in captured), "must query normalised number, not raw CF"


def test_error_intake_no_recoverable_number_bails_to_llm():
    state = {"ar_intake": json.dumps({"__error__": "extract_failed"}),
             "intake_pdf_text": "Sales Order 679424 no cf number here"}
    result, captured = _call_gate(state)
    assert result is None, "no recoverable number -> bail to LLM (unchanged)"
    assert captured == [], "must not query PMI when nothing recoverable"


def test_ambiguous_two_cf_bails_to_llm():
    state = {"ar_intake": json.dumps({"__error__": "extract_failed"}),
             "intake_pdf_text": "CF101159 and also CF100688 on the same doc"}
    result, captured = _call_gate(state)
    assert result is None
    assert captured == []


def test_deliberate_skip_not_second_guessed():
    state = {"ar_intake": json.dumps({"status": "skip"}),
             "intake_pdf_text": "CF101159"}
    result, captured = _call_gate(state)
    assert result is None
    assert captured == []

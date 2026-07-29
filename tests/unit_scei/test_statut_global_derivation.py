"""TÂCHE 3 — deterministic StatutGlobal derivation in tool_persist_ar_record.

The recorder LLM must NOT compute StatutGlobal: the value is DERIVED from
ar_match.status (read from tool_context.state['ar_match']) + the TypeEcart
of the extracted lines. The derived value overrides the LLM proposal; a
WARNING is logged when they diverge (observability).
"""
from __future__ import annotations

import json as _json
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from th2customers.scei.tools import scei_ar_persist as P
from th2customers.scei.tools.scei_ar_persist import _derive_statut_global


def _line(type_ecart):
    return {"TypeEcart": type_ecart}


# --- ar_match status table ---------------------------------------------------


def test_matched_no_ecart_is_conforme():
    assert _derive_statut_global("matched", [_line(None), _line(None)]) == "conforme"


def test_matched_with_ecart_prix_is_non_conforme():
    assert (
        _derive_statut_global("matched", [_line(None), _line("ecart_prix")])
        == "non_conforme"
    )


def test_matched_with_ecart_qte_is_non_conforme():
    assert _derive_statut_global("matched", [_line("ecart_qte")]) == "non_conforme"


def test_matched_with_ecart_date_is_non_conforme():
    assert _derive_statut_global("matched", [_line("ecart_date")]) == "non_conforme"


def test_matched_with_line_not_in_po_is_non_rapproche():
    assert (
        _derive_statut_global("matched", [_line(None), _line("LINE_NOT_IN_PO")])
        == "non_rapproche"
    )


def test_matched_with_typeecart_non_rapproche_is_non_rapproche():
    assert (
        _derive_statut_global("matched", [_line("non_rapproche")]) == "non_rapproche"
    )


def test_matched_with_ligne_absente_prefix_is_non_rapproche():
    assert (
        _derive_statut_global("matched", [_line("ligne_absente_po")])
        == "non_rapproche"
    )


def test_non_rapproche_status_is_non_rapproche():
    assert _derive_statut_global("non_rapproche", [_line(None)]) == "non_rapproche"


def test_out_of_scope_status_is_non_rapproche():
    assert _derive_statut_global("out_of_scope", [_line(None)]) == "non_rapproche"


def test_ar_match_absent_is_non_rapproche():
    assert _derive_statut_global(None, [_line(None)]) == "non_rapproche"


def test_error_status_is_non_rapproche():
    assert _derive_statut_global("__error__", [_line(None)]) == "non_rapproche"


def test_unknown_status_is_non_rapproche():
    assert _derive_statut_global("whatever", [_line(None)]) == "non_rapproche"


def test_non_rapproche_takes_precedence_over_ecart():
    # matched but one line missing in PO AND another line has a price gap
    lignes = [_line("ecart_prix"), _line("LINE_NOT_IN_PO")]
    assert _derive_statut_global("matched", lignes) == "non_rapproche"


def test_empty_lines_matched_is_non_rapproche():
    # matched but no lines extracted is NOT a legitimate conforme
    assert _derive_statut_global("matched", []) == "non_rapproche"


def test_none_lines_matched_is_non_rapproche():
    assert _derive_statut_global("matched", None) == "non_rapproche"


def test_matched_line_nok_unrecognized_ecart_is_non_conforme():
    # Situation=NOK but TypeEcart is NOT in the known vocabulary: must NOT
    # fall through to conforme (faux-conforme guard).
    assert (
        _derive_statut_global("matched", [{"Situation": "NOK", "TypeEcart": "ecart_delai"}])
        == "non_conforme"
    )


def test_matched_line_nok_empty_typeecart_is_non_conforme():
    assert (
        _derive_statut_global("matched", [{"Situation": "NOK", "TypeEcart": None}])
        == "non_conforme"
    )


def test_matched_line_ok_stays_conforme():
    assert (
        _derive_statut_global("matched", [{"Situation": "OK", "TypeEcart": None}])
        == "conforme"
    )


def test_typeecart_case_insensitive():
    assert (
        _derive_statut_global("matched", [_line("ECART_PRIX")]) == "non_conforme"
    )

# ---------------------------------------------------------------------------
# Integration: derivation wired into tool_persist_ar_record (override + WARNING)
# ---------------------------------------------------------------------------


class _FakeConn:
    def __init__(self, captured):
        self._captured = captured

    def execute(self, stmt, params=None):
        if isinstance(params, dict) and "StatutGlobal" in params:
            self._captured["header_params"] = params
        res = MagicMock()
        res.scalar.return_value = 42  # SCOPE_IDENTITY / idempotency precheck
        return res

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeEngine:
    def __init__(self, captured):
        self._captured = captured
        # idempotency precheck must return None (no existing row)
        self._precheck = True

    def connect(self):
        # idempotency precheck path -> scalar None
        conn = MagicMock()
        cm = MagicMock()
        res = MagicMock()
        res.scalar.return_value = None
        conn.execute.return_value = res
        cm.__enter__.return_value = conn
        cm.__exit__.return_value = False
        return cm

    @contextmanager
    def begin(self):
        yield _FakeConn(self._captured)


def _state_ctx(ar_match_json):
    ctx = MagicMock()
    ctx.state = {"ar_match": ar_match_json}
    return ctx


def test_persist_overrides_llm_statut_with_derived(caplog):
    """LLM proposes 'conforme' but a line is LINE_NOT_IN_PO and ar_match is
    matched -> derived 'non_rapproche' is what gets INSERTed, plus a WARNING."""
    captured = {}
    fake_engine = _FakeEngine(captured)
    ar_match = _json.dumps({"status": "matched", "diagnostic": "ok"})

    with patch.object(P, "_get_db_engine", return_value=fake_engine):
        with caplog.at_level("WARNING"):
            result = P.tool_persist_ar_record(
                NumeroCommande="CF100688",
                DateCommande="2026-05-26",
                StatutGlobal="conforme",  # LLM proposal (wrong)
                lignes=[{"TypeEcart": "LINE_NOT_IN_PO"}],
                tool_context=_state_ctx(ar_match),
            )

    assert result["success"] is True
    assert captured["header_params"]["StatutGlobal"] == "non_rapproche"
    assert any(
        "derived" in r.message.lower() or "diverg" in r.message.lower()
        for r in caplog.records
    ), f"expected divergence WARNING, got: {[r.message for r in caplog.records]}"


def test_persist_keeps_derived_when_llm_agrees(caplog):
    """LLM proposes 'conforme', matched + no gap -> derived 'conforme', no WARNING."""
    captured = {}
    fake_engine = _FakeEngine(captured)
    ar_match = _json.dumps({"status": "matched", "diagnostic": "ok"})

    with patch.object(P, "_get_db_engine", return_value=fake_engine):
        with caplog.at_level("WARNING"):
            result = P.tool_persist_ar_record(
                NumeroCommande="CF100688",
                DateCommande="2026-05-26",
                StatutGlobal="conforme",
                lignes=[{"TypeEcart": None}],
                tool_context=_state_ctx(ar_match),
            )

    assert result["success"] is True
    assert captured["header_params"]["StatutGlobal"] == "conforme"
    assert not any(
        "diverg" in r.message.lower() for r in caplog.records
    )


def test_persist_ar_match_absent_forces_non_rapproche():
    """No ar_match in state -> derived non_rapproche regardless of LLM."""
    captured = {}
    fake_engine = _FakeEngine(captured)
    ctx = MagicMock()
    ctx.state = {}

    with patch.object(P, "_get_db_engine", return_value=fake_engine):
        result = P.tool_persist_ar_record(
            NumeroCommande="CF100688",
            DateCommande="2026-05-26",
            StatutGlobal="conforme",
            lignes=[{"TypeEcart": None}],
            tool_context=ctx,
        )

    assert result["success"] is True
    assert captured["header_params"]["StatutGlobal"] == "non_rapproche"

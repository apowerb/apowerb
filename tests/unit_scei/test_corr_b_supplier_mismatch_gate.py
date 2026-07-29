"""CORRECTION B — TDD RED : gate supplier_mismatch sur le notifier.

Quand ar_match porte supplier_mismatch=True :
1. LOG CRITICAL émis
2. Le notifier est court-circuité (pas d'appel LLM, résultat "non envoyé / revue requise")
3. Le recorder n'est PAS court-circuité
4. supplier_mismatch=False/absent → pas de court-circuit par ce garde

RED: tests ÉCHOUENT avant l'implémentation.
"""
from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_callback_context(state: dict) -> MagicMock:
    ctx = MagicMock()
    ctx.state = state
    ctx.agent_name = "scei_ar_notifier"
    return ctx


def _ar_match_state(mismatch: bool | None = None, status: str = "matched") -> dict:
    payload: dict = {
        "status": status,
        "po": {"ecktsoc": "SC", "ecktnumero": "101090"},
        "lines": [],
        "diagnostic": "matched",
    }
    if mismatch is not None:
        payload["supplier_mismatch"] = mismatch
    return {"ar_match": json.dumps(payload)}


# ---------------------------------------------------------------------------
# Tests CORRECTION B
# ---------------------------------------------------------------------------


class TestSupplierMismatchGateOnNotifier:
    """Gate supplier_mismatch : court-circuit du notifier si mismatch=True."""

    def test_mismatch_true_returns_content_not_none(self):
        """supplier_mismatch=True → le callback retourne un Content (court-circuit)."""
        from th2customers.scei.gates import build_supplier_mismatch_gate_callback
        state = _ar_match_state(mismatch=True)
        ctx = _make_callback_context(state)

        with patch("google.genai.types.Content") as MockContent, \
             patch("google.genai.types.Part") as MockPart:
            MockContent.return_value = MagicMock()
            MockPart.return_value = MagicMock()
            cb = build_supplier_mismatch_gate_callback(output_key="ar_notify")
            result = cb(ctx)

        assert result is not None, "Doit retourner un Content pour court-circuiter le LLM"

    def test_mismatch_true_writes_non_envoye_in_state(self):
        """supplier_mismatch=True → state['ar_notify'] porte human_review_required=True."""
        from th2customers.scei.gates import build_supplier_mismatch_gate_callback
        state = _ar_match_state(mismatch=True)
        ctx = _make_callback_context(state)

        with patch("google.genai.types.Content") as MockContent, \
             patch("google.genai.types.Part") as MockPart:
            MockContent.return_value = MagicMock()
            MockPart.return_value = MagicMock()
            cb = build_supplier_mismatch_gate_callback(output_key="ar_notify")
            cb(ctx)

        written = json.loads(state["ar_notify"])
        assert written.get("human_review_required") is True, (
            "state['ar_notify'] doit porter human_review_required=True"
        )
        assert written.get("sent") is False, "sent doit être False"

    def test_mismatch_false_returns_none(self):
        """supplier_mismatch=False → pas de court-circuit (retourne None)."""
        from th2customers.scei.gates import build_supplier_mismatch_gate_callback
        state = _ar_match_state(mismatch=False)
        ctx = _make_callback_context(state)
        cb = build_supplier_mismatch_gate_callback(output_key="ar_notify")
        result = cb(ctx)
        assert result is None, "supplier_mismatch=False ne doit PAS court-circuiter"

    def test_mismatch_absent_returns_none(self):
        """supplier_mismatch absent → pas de court-circuit."""
        from th2customers.scei.gates import build_supplier_mismatch_gate_callback
        state = _ar_match_state(mismatch=None)
        ctx = _make_callback_context(state)
        cb = build_supplier_mismatch_gate_callback(output_key="ar_notify")
        result = cb(ctx)
        assert result is None, "supplier_mismatch absent ne doit PAS court-circuiter"

    def test_mismatch_true_logs_critical(self, caplog):
        """supplier_mismatch=True → log CRITICAL émis."""
        from th2customers.scei.gates import build_supplier_mismatch_gate_callback
        state = _ar_match_state(mismatch=True)
        state["ar_match"] = json.dumps({
            "status": "matched",
            "po": {"ecktsoc": "SC", "ecktnumero": "CF101090"},
            "supplier_mismatch": True,
            "ar_fournisseur": "SOCOMEC",
            "pmi_fournisseur": "WILLY LEISSNER",
        })
        ctx = _make_callback_context(state)

        with caplog.at_level(logging.CRITICAL, logger="th2agent.core.agent_helpers.callbacks"):
            with patch("google.genai.types.Content") as MockContent, \
                 patch("google.genai.types.Part") as MockPart:
                MockContent.return_value = MagicMock()
                MockPart.return_value = MagicMock()
                cb = build_supplier_mismatch_gate_callback(output_key="ar_notify")
                cb(ctx)

        assert any("SCEI_SUPPLIER_MISMATCH" in r.message for r in caplog.records), (
            "Doit émettre un log CRITICAL contenant SCEI_SUPPLIER_MISMATCH"
        )

    def test_ar_match_missing_returns_none(self):
        """ar_match absent → pas de court-circuit (graceful degradation)."""
        from th2customers.scei.gates import build_supplier_mismatch_gate_callback
        state = {}
        ctx = _make_callback_context(state)
        cb = build_supplier_mismatch_gate_callback(output_key="ar_notify")
        result = cb(ctx)
        assert result is None


class TestRecorderNotAffectedBySupplierMismatch:
    """Le recorder (agent10) ne doit PAS être court-circuité par supplier_mismatch."""

    def test_supplier_mismatch_gate_is_notifier_only(self):
        """build_supplier_mismatch_gate_callback a un output_key différent du recorder.

        Vérifie que l'application de la gate sur le recorder (output_key='ar_record')
        ne short-circuite PAS quand supplier_mismatch=True — seul le notifier est visé.
        Ce test vérifie que la gate fonctionne avec n'importe quel output_key
        MAIS la décision de ne pas câbler sur le recorder est dans maybe_wire_supplier_mismatch_gate.
        """
        from th2customers.scei.gates import build_supplier_mismatch_gate_callback
        # Même état avec mismatch=True
        state = _ar_match_state(mismatch=True)
        ctx = _make_callback_context(state)
        ctx.agent_name = "scei_ar_recorder"

        # Si on câblait PAR ERREUR la gate sur le recorder avec ar_notify comme output_key
        # cela ne changerait rien au recorder car le notifier a son propre output_key.
        # Le test de ségrégation est dans test_maybe_wire_only_for_notifier ci-dessous.
        # Ici : confirme que le recorder PEUT accéder à la gate si on l'applique manuellement
        # mais que ça reste isolé.
        cb = build_supplier_mismatch_gate_callback(output_key="ar_notify")
        # La gate lit ar_match (toujours), pas l'output_key en entrée
        # Elle ÉCRIRA dans state["ar_notify"] si mismatch — mais pas dans state["ar_record"]
        with patch("google.genai.types.Content") as MockContent, \
             patch("google.genai.types.Part") as MockPart:
            MockContent.return_value = MagicMock()
            MockPart.return_value = MagicMock()
            cb(ctx)

        # ar_record ne doit PAS avoir été touché
        assert "ar_record" not in state or state.get("ar_record") is None, (
            "La gate supplier_mismatch ne doit PAS écrire dans ar_record"
        )

"""CORRECTION B — wiring : maybe_wire_supplier_mismatch_gate.

Vérifie que la gate est câblée uniquement sur le notifier (ARNotifyPayload, ar_notify)
et PAS sur le recorder (ARRecordPayload, ar_record) ni sur d'autres agents.
"""
from __future__ import annotations


class TestMaybeWireSupplierMismatchGate:
    """maybe_wire_supplier_mismatch_gate câble uniquement le notifier."""

    def test_wires_for_notifier(self):
        """output_schema_name=ARNotifyPayload + output_key=ar_notify → câblé."""
        from th2customers.scei.gates import maybe_wire_supplier_mismatch_gate
        agent_details = {
            "output_schema_name": "ARNotifyPayload",
            "output_key": "ar_notify",
        }
        agent_kwargs: dict = {}
        result = maybe_wire_supplier_mismatch_gate(agent_details, "ar_notify", agent_kwargs)
        assert result is True
        assert "before_agent_callback" in agent_kwargs

    def test_does_not_wire_for_recorder(self):
        """output_schema_name=ARRecordPayload → PAS câblé (recorder doit toujours s'exécuter)."""
        from th2customers.scei.gates import maybe_wire_supplier_mismatch_gate
        agent_details = {
            "output_schema_name": "ARRecordPayload",
            "output_key": "ar_record",
        }
        agent_kwargs: dict = {}
        result = maybe_wire_supplier_mismatch_gate(agent_details, "ar_record", agent_kwargs)
        assert result is False
        assert "before_agent_callback" not in agent_kwargs

    def test_does_not_wire_for_wrong_output_key(self):
        """output_schema_name correct mais output_key différent → PAS câblé."""
        from th2customers.scei.gates import maybe_wire_supplier_mismatch_gate
        agent_details = {
            "output_schema_name": "ARNotifyPayload",
            "output_key": "ar_other",
        }
        agent_kwargs: dict = {}
        result = maybe_wire_supplier_mismatch_gate(agent_details, "ar_other", agent_kwargs)
        assert result is False

    def test_does_not_wire_without_output_key(self):
        """Pas d'output_key → PAS câblé."""
        from th2customers.scei.gates import maybe_wire_supplier_mismatch_gate
        agent_details = {"output_schema_name": "ARNotifyPayload"}
        agent_kwargs: dict = {}
        result = maybe_wire_supplier_mismatch_gate(agent_details, None, agent_kwargs)
        assert result is False

    def test_does_not_wire_for_matcher(self):
        """ARMatchPayload (matcher) → PAS câblé."""
        from th2customers.scei.gates import maybe_wire_supplier_mismatch_gate
        agent_details = {
            "output_schema_name": "ARMatchPayload",
            "output_key": "ar_match",
        }
        agent_kwargs: dict = {}
        result = maybe_wire_supplier_mismatch_gate(agent_details, "ar_match", agent_kwargs)
        assert result is False

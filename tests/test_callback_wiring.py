"""Tests for Fix E — the after_agent_callback wiring chain.

This proves the chain `template.output_schema_name → registry lookup →
build_validating_state_writer(schema_class, output_key)` actually
resolves, so the callback isn't dead code at runtime."""

from __future__ import annotations

import pytest


class TestSchemaLookupChain:
    def test_lookup_resolves_scei_schemas(self):
        from th2agent.core.agent_helpers.agent_utils import (
            _lookup_output_schema,
        )
        from th2customers.scei.schemas import (
            ARIntakePayload,
            SCEIIntakePayload,
            ARMatchPayload,
            ARRecordPayload,
            ARNotifyPayload,
        )

        assert _lookup_output_schema("ARIntakePayload") is ARIntakePayload
        assert _lookup_output_schema("SCEIIntakePayload") is SCEIIntakePayload
        assert _lookup_output_schema("ARMatchPayload") is ARMatchPayload
        assert _lookup_output_schema("ARRecordPayload") is ARRecordPayload
        assert _lookup_output_schema("ARNotifyPayload") is ARNotifyPayload

    def test_lookup_returns_none_for_unknown(self):
        from th2agent.core.agent_helpers.agent_utils import (
            _lookup_output_schema,
        )

        assert _lookup_output_schema("BogusPayload") is None

    def test_every_scei_v2_subagent_output_schema_resolves(self):
        """The 4 SCEI sub-agent templates declare an output_schema_name;
        each must resolve to a real Pydantic class via the lookup chain.
        If this test fails, the after_agent_callback won't get wired and
        downstream sub-agents will read raw LLM prose, not validated JSON."""
        from th2agent.core.agent_helpers.agent_utils import (
            _lookup_output_schema,
        )
        from th2customers.scei.templates.scei_v2 import (
            SCEI_AR_INTAKE,
            SCEI_AR_MATCHER,
            SCEI_AR_RECORDER,
            SCEI_AR_NOTIFIER,
        )

        for tpl in (
            SCEI_AR_INTAKE,
            SCEI_AR_MATCHER,
            SCEI_AR_RECORDER,
            SCEI_AR_NOTIFIER,
        ):
            name = tpl["output_schema_name"]
            cls = _lookup_output_schema(name)
            assert cls is not None, (
                f"Template {tpl['name']} declares output_schema_name="
                f"{name!r} but it does not resolve to any registered "
                f"Pydantic class"
            )

    def test_callback_factory_accepts_resolved_class(self):
        """End-to-end: name → class → callback built without error."""
        from th2agent.core.agent_helpers.agent_utils import (
            _lookup_output_schema,
        )
        from th2agent.core.agent_helpers.callbacks import (
            build_validating_state_writer,
        )

        cls = _lookup_output_schema("ARIntakePayload")
        cb = build_validating_state_writer(cls, "ar_intake")
        assert callable(cb)
        # ADK convention: callback name is informative
        assert "ARIntakePayload" in cb.__name__
        assert "ar_intake" in cb.__name__


class TestAttachmentGateWiring:
    """Step 2 — the attachment_pdf_gate template flag must wire a
    before_agent_callback (the deterministic no-PDF gate)."""

    def test_wired_when_flag_and_output_key(self):
        from th2customers.scei.gates import maybe_wire_attachment_pdf_gate

        kwargs: dict = {}
        wired = maybe_wire_attachment_pdf_gate(
            {"attachment_pdf_gate": True}, "ar_intake", kwargs
        )
        assert wired is True
        assert "before_agent_callback" in kwargs
        assert (
            kwargs["before_agent_callback"].__name__
            == "attachment_pdf_gate_writes_ar_intake"
        )

    def test_not_wired_without_flag(self):
        from th2customers.scei.gates import maybe_wire_attachment_pdf_gate

        kwargs: dict = {}
        assert maybe_wire_attachment_pdf_gate({}, "ar_intake", kwargs) is False
        assert "before_agent_callback" not in kwargs

    def test_wired_via_output_schema_name(self):
        """Prod path: SCEIIntakePayload schema activates the gate (no flag,
        no DB column needed — output_schema_name is restored by resync)."""
        from th2customers.scei.gates import maybe_wire_attachment_pdf_gate

        kwargs: dict = {}
        wired = maybe_wire_attachment_pdf_gate(
            {"output_schema_name": "SCEIIntakePayload"}, "ar_intake", kwargs
        )
        assert wired is True
        assert kwargs["before_agent_callback"].__name__ == (
            "attachment_pdf_gate_writes_ar_intake"
        )

    def test_not_wired_for_other_schema(self):
        from th2customers.scei.gates import maybe_wire_attachment_pdf_gate

        kwargs: dict = {}
        assert maybe_wire_attachment_pdf_gate(
            {"output_schema_name": "ARMatchPayload"}, "ar_match", kwargs
        ) is False
        assert "before_agent_callback" not in kwargs

    def test_not_wired_without_output_key(self):
        from th2customers.scei.gates import maybe_wire_attachment_pdf_gate

        kwargs: dict = {}
        assert (
            maybe_wire_attachment_pdf_gate(
                {"attachment_pdf_gate": True}, None, kwargs
            )
            is False
        )
        assert "before_agent_callback" not in kwargs

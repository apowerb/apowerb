"""Tests for Fix E — the after_agent_callback wiring chain.

The lookup half of `template.output_schema_name → registry lookup →
build_validating_state_writer(schema_class, output_key)`. The schemas a
client overlay registers, and the gates it wires, are tested with that
overlay, outside this repository."""

from __future__ import annotations


class TestSchemaLookupChain:
    def test_lookup_returns_none_for_unknown(self):
        from apowerb.core.agent_helpers.agent_utils import (
            _lookup_output_schema,
        )

        assert _lookup_output_schema("BogusPayload") is None

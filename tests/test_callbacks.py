"""Tests for SCEI sub-agent communication callbacks.

The factory ``build_validating_state_writer(schema_class, state_key)``
returns an ADK ``after_agent_callback`` that:

1. Reads the agent's text output from ``session.state[state_key]`` (where
   ADK already wrote it via ``output_key``).
2. Extracts a JSON block (raw, markdown-fenced, or trailing in prose).
3. Validates it against the Pydantic ``schema_class``.
4. Rewrites ``state[state_key]`` as ``json.dumps(model.model_dump())`` so
   the next sub-agent reads a stable JSON string (ADK's ``str()`` of a
   dict would produce Python ``repr``, not JSON — see
   [[feedback_adk_brace_escape_pitfall]] and PR #173 critique).

If extraction or validation fails, an error sentinel payload is written
so downstream sub-agents can detect the failure via
``before_model_callback`` without raising at session-level (which would
abort the SequentialAgent mid-pipeline)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Helper schema for tests
# ---------------------------------------------------------------------------


class _TestPayload(BaseModel):
    status: str
    n: int = 0


def _make_ctx(state: dict | None = None):
    """Minimal ADK CallbackContext-shaped mock for the callback to operate on."""
    ctx = MagicMock()
    state_dict = dict(state or {})

    class _State:
        def __init__(self, d):
            self._d = d

        def get(self, k, default=None):
            return self._d.get(k, default)

        def __setitem__(self, k, v):
            self._d[k] = v

        def __getitem__(self, k):
            return self._d[k]

        def __contains__(self, k):
            return k in self._d

    ctx.state = _State(state_dict)
    return ctx, state_dict


# ---------------------------------------------------------------------------
# extract_json — pure helper
# ---------------------------------------------------------------------------


class TestExtractJson:
    def test_pure_json_string(self):
        from th2agent.core.agent_helpers.callbacks import extract_json

        assert extract_json('{"status": "ok"}') == {"status": "ok"}

    def test_markdown_fenced(self):
        from th2agent.core.agent_helpers.callbacks import extract_json

        text = 'Here you go:\n```json\n{"status": "ok"}\n```\nDone.'
        assert extract_json(text) == {"status": "ok"}

    def test_trailing_in_prose(self):
        from th2agent.core.agent_helpers.callbacks import extract_json

        text = 'Analysis complete.\n\n{"status": "process", "n": 3}'
        assert extract_json(text) == {"status": "process", "n": 3}

    def test_multiple_blocks_returns_last(self):
        """An LLM may show an example then the real payload — keep the last."""
        from th2agent.core.agent_helpers.callbacks import extract_json

        text = 'Example: {"status": "skip"}\nFinal: {"status": "process", "n": 5}'
        assert extract_json(text) == {"status": "process", "n": 5}

    def test_invalid_json_returns_none(self):
        from th2agent.core.agent_helpers.callbacks import extract_json

        assert extract_json("no braces here at all") is None
        assert extract_json("") is None
        assert extract_json("{ this isn't json }") is None

    def test_nested_object(self):
        from th2agent.core.agent_helpers.callbacks import extract_json

        text = '{"status": "ok", "data": {"k": "v"}}'
        assert extract_json(text) == {"status": "ok", "data": {"k": "v"}}


# ---------------------------------------------------------------------------
# build_validating_state_writer — the factory
# ---------------------------------------------------------------------------


class TestValidatingStateWriter:
    @pytest.mark.asyncio
    async def test_valid_json_in_state_is_normalized(self):
        from th2agent.core.agent_helpers.callbacks import (
            build_validating_state_writer,
        )

        cb = build_validating_state_writer(_TestPayload, "out")
        ctx, state = _make_ctx({"out": '{"status": "ok", "n": 7}'})

        result = await cb(callback_context=ctx)

        # State must be rewritten as canonical json.dumps
        assert state["out"] == json.dumps({"status": "ok", "n": 7})
        # Callback returns None (no Content override)
        assert result is None

    @pytest.mark.asyncio
    async def test_json_with_prose_extracted_and_normalized(self):
        from th2agent.core.agent_helpers.callbacks import (
            build_validating_state_writer,
        )

        cb = build_validating_state_writer(_TestPayload, "out")
        text = 'Done.\n\n{"status": "ok", "n": 1}\n\nAny questions?'
        ctx, state = _make_ctx({"out": text})

        await cb(callback_context=ctx)
        assert json.loads(state["out"]) == {"status": "ok", "n": 1}

    @pytest.mark.asyncio
    async def test_invalid_json_writes_error_sentinel(self):
        from th2agent.core.agent_helpers.callbacks import (
            build_validating_state_writer,
        )

        cb = build_validating_state_writer(_TestPayload, "out")
        ctx, state = _make_ctx({"out": "Sorry I forgot the JSON."})

        await cb(callback_context=ctx)

        # State now holds a structured error sentinel so downstream can
        # detect the failure without raising mid-SequentialAgent.
        decoded = json.loads(state["out"])
        assert decoded["__error__"] == "extract_failed"
        assert "raw_text_preview" in decoded

    @pytest.mark.asyncio
    async def test_schema_violation_writes_error_sentinel(self):
        from th2agent.core.agent_helpers.callbacks import (
            build_validating_state_writer,
        )

        cb = build_validating_state_writer(_TestPayload, "out")
        # `n` should be int, "not-int" fails validation
        ctx, state = _make_ctx({"out": '{"status": "ok", "n": "not-int"}'})

        await cb(callback_context=ctx)

        decoded = json.loads(state["out"])
        assert decoded["__error__"] == "validation_failed"
        assert "errors" in decoded

    @pytest.mark.asyncio
    async def test_missing_state_key_writes_error_sentinel(self):
        from th2agent.core.agent_helpers.callbacks import (
            build_validating_state_writer,
        )

        cb = build_validating_state_writer(_TestPayload, "out")
        ctx, state = _make_ctx({})  # 'out' not present

        await cb(callback_context=ctx)

        decoded = json.loads(state["out"])
        assert decoded["__error__"] == "missing_output"

    @pytest.mark.asyncio
    async def test_callback_is_callable_via_kwarg_only(self):
        """ADK passes callback_context as kwarg, not positional."""
        from th2agent.core.agent_helpers.callbacks import (
            build_validating_state_writer,
        )

        cb = build_validating_state_writer(_TestPayload, "out")
        ctx, _ = _make_ctx({"out": '{"status": "ok"}'})

        # Calling positionally should still work but kwarg is canonical
        result = await cb(callback_context=ctx)
        assert result is None


# ---------------------------------------------------------------------------
# is_upstream_skip — helper for downstream short-circuit
# ---------------------------------------------------------------------------


class TestIsUpstreamSkip:
    def test_skip_status_detected(self):
        from th2agent.core.agent_helpers.callbacks import is_upstream_skip

        state = {"ar_intake": json.dumps({"status": "skip", "raison": "x"})}
        assert is_upstream_skip(state, "ar_intake") is True

    def test_process_status_not_skip(self):
        from th2agent.core.agent_helpers.callbacks import is_upstream_skip

        state = {"ar_intake": json.dumps({"status": "process"})}
        assert is_upstream_skip(state, "ar_intake") is False

    def test_missing_key_not_skip(self):
        from th2agent.core.agent_helpers.callbacks import is_upstream_skip

        assert is_upstream_skip({}, "ar_intake") is False

    def test_error_sentinel_not_skip(self):
        """An upstream error is not a 'skip' — downstream may want different
        behavior (e.g. abort) but is_upstream_skip stays narrow."""
        from th2agent.core.agent_helpers.callbacks import is_upstream_skip

        state = {"ar_intake": json.dumps({"__error__": "extract_failed"})}
        assert is_upstream_skip(state, "ar_intake") is False

    def test_not_ar_classification_is_skip(self):
        """Intake v2: email_classification == 'not_ar' is a skip signal."""
        from th2agent.core.agent_helpers.callbacks import is_upstream_skip

        state = {"ar_intake": json.dumps(
            {"email_classification": "not_ar", "raison": "facture",
             "extraction_results": {}})}
        assert is_upstream_skip(state, "ar_intake") is True

    def test_ar_classification_is_not_skip(self):
        from th2agent.core.agent_helpers.callbacks import is_upstream_skip

        state = {"ar_intake": json.dumps(
            {"email_classification": "ar",
             "extraction_results": {"commande_number_sql": "101082"}})}
        assert is_upstream_skip(state, "ar_intake") is False


class TestIsUpstreamAbsentOrError:
    """Companion to is_upstream_skip: catches the *missing/errored* upstream
    case so the downstream short-circuit never lets the LLM improvise on an
    empty payload (regression: 2026-05-21 ar_intake KeyError loop)."""

    def test_missing_key_is_absent(self):
        from th2agent.core.agent_helpers.callbacks import is_upstream_absent_or_error

        assert is_upstream_absent_or_error({}, "ar_intake") is True

    def test_empty_value_is_absent(self):
        from th2agent.core.agent_helpers.callbacks import is_upstream_absent_or_error

        assert is_upstream_absent_or_error({"ar_intake": ""}, "ar_intake") is True

    def test_error_sentinel_detected(self):
        from th2agent.core.agent_helpers.callbacks import is_upstream_absent_or_error

        state = {"ar_intake": json.dumps({"__error__": "extract_failed"})}
        assert is_upstream_absent_or_error(state, "ar_intake") is True

    def test_valid_payload_is_not_absent_or_error(self):
        from th2agent.core.agent_helpers.callbacks import is_upstream_absent_or_error

        state = {"ar_intake": json.dumps({"status": "process"})}
        assert is_upstream_absent_or_error(state, "ar_intake") is False

    def test_skip_payload_is_not_error(self):
        from th2agent.core.agent_helpers.callbacks import is_upstream_absent_or_error

        state = {"ar_intake": json.dumps({"status": "skip"})}
        assert is_upstream_absent_or_error(state, "ar_intake") is False

    def test_non_json_present_left_to_downstream(self):
        from th2agent.core.agent_helpers.callbacks import is_upstream_absent_or_error

        assert is_upstream_absent_or_error({"ar_intake": "garbage"}, "ar_intake") is False

    def test_not_ar_is_not_error(self):
        """not_ar is a valid classification, NOT an error sentinel."""
        from th2agent.core.agent_helpers.callbacks import is_upstream_absent_or_error

        state = {"ar_intake": json.dumps({"email_classification": "not_ar"})}
        assert is_upstream_absent_or_error(state, "ar_intake") is False


class TestSkipShortCircuitOnAbsentOrError:
    """The before_model_callback must short-circuit not only on an explicit
    skip, but also when the upstream key is absent/errored — otherwise the
    matcher runs its LLM on an empty `{ar_intake?}` and may hallucinate a PO."""

    async def test_short_circuits_when_upstream_absent(self):
        from th2agent.core.agent_helpers.callbacks import (
            build_skip_short_circuit_callback,
        )

        cb = build_skip_short_circuit_callback("ar_intake", "ar_match")
        ctx = MagicMock()
        ctx.state = {}
        await cb(callback_context=ctx)
        assert "__skipped_upstream__" in ctx.state["ar_match"]

    async def test_short_circuits_on_error_sentinel(self):
        from th2agent.core.agent_helpers.callbacks import (
            build_skip_short_circuit_callback,
        )

        cb = build_skip_short_circuit_callback("ar_intake", "ar_match")
        ctx = MagicMock()
        ctx.state = {"ar_intake": json.dumps({"__error__": "missing_output"})}
        await cb(callback_context=ctx)
        assert "__skipped_upstream__" in ctx.state["ar_match"]

    async def test_does_not_short_circuit_on_valid_process(self):
        from th2agent.core.agent_helpers.callbacks import (
            build_skip_short_circuit_callback,
        )

        cb = build_skip_short_circuit_callback("ar_intake", "ar_match")
        ctx = MagicMock()
        ctx.state = {"ar_intake": json.dumps({"status": "process"})}
        resp = await cb(callback_context=ctx)
        assert resp is None
        assert "ar_match" not in ctx.state

    async def test_cascade_from_llm_not_ar_classification(self):
        """Intake LLM classifies not_ar (SCEIIntakePayload) -> matcher
        short-circuits -> recorder short-circuits (full v2 cascade)."""
        from th2agent.core.agent_helpers.callbacks import (
            build_skip_short_circuit_callback,
        )

        ctx = MagicMock()
        ctx.state = {"ar_intake": json.dumps(
            {"email_classification": "not_ar", "raison": "facture",
             "extraction_results": {}})}
        await build_skip_short_circuit_callback("ar_intake", "ar_match")(
            callback_context=ctx
        )
        assert "__skipped_upstream__" in ctx.state["ar_match"]
        await build_skip_short_circuit_callback("ar_match", "ar_record")(
            callback_context=ctx
        )
        assert "__skipped_upstream__" in ctx.state["ar_record"]

    async def test_cascade_skip_propagates_through_absent_upstream(self):
        """Intake absent -> matcher writes __skipped_upstream__ into ar_match
        -> recorder must ALSO short-circuit (cascade), via is_upstream_skip
        detecting the sentinel."""
        from th2agent.core.agent_helpers.callbacks import (
            build_skip_short_circuit_callback,
        )

        # Step 1: matcher short-circuits on absent ar_intake.
        matcher_cb = build_skip_short_circuit_callback("ar_intake", "ar_match")
        ctx = MagicMock()
        ctx.state = {}
        await matcher_cb(callback_context=ctx)
        assert "__skipped_upstream__" in ctx.state["ar_match"]

        # Step 2: recorder sees the cascade sentinel in ar_match and skips too.
        recorder_cb = build_skip_short_circuit_callback("ar_match", "ar_record")
        await recorder_cb(callback_context=ctx)
        assert "__skipped_upstream__" in ctx.state["ar_record"]

class TestExtractJsonNested:
    def test_nesting_depth_2_preserved(self):
        from th2agent.core.agent_helpers.callbacks import extract_json

        result = extract_json('{"a": {"b": {"c": 1}}}')
        assert result == {"a": {"b": {"c": 1}}}

    def test_nesting_depth_3_preserved(self):
        from th2agent.core.agent_helpers.callbacks import extract_json

        text = '{"l1": {"l2": {"l3": {"l4": "deep"}}}}'
        result = extract_json(text)
        assert result == {"l1": {"l2": {"l3": {"l4": "deep"}}}}

    def test_array_only_returns_none(self):
        """`[1, 2, 3]` is JSON but not a dict — return None so caller
        knows to surface 'extract_failed' instead of validating."""
        from th2agent.core.agent_helpers.callbacks import extract_json

        assert extract_json("[1, 2, 3]") is None

    def test_array_inside_object_works(self):
        from th2agent.core.agent_helpers.callbacks import extract_json

        result = extract_json('{"lines": [{"id": 1}, {"id": 2}]}')
        assert result == {"lines": [{"id": 1}, {"id": 2}]}

    def test_empty_object_in_prose_treated_as_skipped(self):
        """`{}` appearing as example/placeholder shouldn't preempt the
        real payload that comes after."""
        from th2agent.core.agent_helpers.callbacks import extract_json

        text = 'Example: {} (empty)\nReal: {"status": "ok"}'
        result = extract_json(text)
        assert result == {"status": "ok"}

    def test_only_empty_object_returns_empty(self):
        """If the only object found is {}, return it (caller will fail
        validation downstream — that is the right behavior)."""
        from th2agent.core.agent_helpers.callbacks import extract_json

        assert extract_json("Here: {}") == {}

    def test_dict_with_string_containing_braces(self):
        """String values can contain `{` or `}` — JSON decoder handles."""
        from th2agent.core.agent_helpers.callbacks import extract_json

        result = extract_json('{"msg": "use {{xxx}} as escape"}')
        assert result == {"msg": "use {{xxx}} as escape"}

    def test_unbalanced_braces_returns_none(self):
        from th2agent.core.agent_helpers.callbacks import extract_json

        assert extract_json("{ this isn't json") is None
        assert extract_json("{") is None


class TestAttachmentPdfGate:
    """Deterministic intake gate: no PDF attachment -> not_ar, no LLM."""

    def test_is_pdf_attachment_helper(self):
        from th2agent.core.agent_helpers.callbacks import _is_pdf_attachment

        assert _is_pdf_attachment({"content_type": "application/pdf"}) is True
        assert _is_pdf_attachment({"filename": "AR_CF101082.PDF"}) is True
        assert _is_pdf_attachment(
            {"content_type": "image/png", "filename": "signature.png"}
        ) is False
        assert _is_pdf_attachment({}) is False
        assert _is_pdf_attachment("not a dict") is False
        assert _is_pdf_attachment({"content_type": None, "filename": None}) is False

    def test_no_pdf_short_circuits_to_not_ar(self):
        from th2customers.scei.gates import build_attachment_pdf_gate_callback

        cb = build_attachment_pdf_gate_callback("ar_intake")
        ctx = MagicMock()
        ctx.state = {"attachments": [
            {"content_type": "image/png", "filename": "logo.png"},
        ]}
        res = cb(callback_context=ctx)
        payload = json.loads(ctx.state["ar_intake"])
        assert payload["email_classification"] == "not_ar"
        assert payload["raison"] == "no_pdf_attachment"
        assert payload["extraction_results"] == {}
        assert res is not None  # short-circuit content returned (skips LLM)

    def test_with_pdf_proceeds_to_llm(self):
        from th2customers.scei.gates import build_attachment_pdf_gate_callback

        cb = build_attachment_pdf_gate_callback("ar_intake")
        ctx = MagicMock()
        ctx.state = {"attachments": [
            {"content_type": "image/png", "filename": "sig.png"},
            {"content_type": "application/pdf", "filename": "ar.pdf"},
        ]}
        res = cb(callback_context=ctx)
        assert res is None  # PDF present -> let the LLM run
        assert "ar_intake" not in ctx.state

    def test_no_attachments_key_is_not_ar(self):
        from th2customers.scei.gates import build_attachment_pdf_gate_callback

        cb = build_attachment_pdf_gate_callback("ar_intake")
        ctx = MagicMock()
        ctx.state = {}
        cb(callback_context=ctx)
        assert json.loads(ctx.state["ar_intake"])["email_classification"] == "not_ar"

    def test_written_payload_revalidates(self):
        """The short-circuit JSON must be a valid SCEIIntakePayload (the
        downstream contract)."""
        from th2customers.scei.gates import build_attachment_pdf_gate_callback
        from th2customers.scei.schemas import SCEIIntakePayload

        cb = build_attachment_pdf_gate_callback("ar_intake")
        ctx = MagicMock()
        ctx.state = {"attachments": []}
        cb(callback_context=ctx)
        p = SCEIIntakePayload.model_validate(json.loads(ctx.state["ar_intake"]))
        assert p.email_classification == "not_ar"

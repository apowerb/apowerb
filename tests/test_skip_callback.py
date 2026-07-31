"""Tests for build_skip_short_circuit_callback — the Phase 2a skip path.

A SequentialAgent's downstream sub-agents (matcher, recorder, notifier)
must short-circuit when intake returned ``status: 'skip'``. Without
this, each downstream still calls the LLM (cost + latency) on a payload
it cannot meaningfully process.

The callback runs as ADK ``before_model_callback`` — it reads upstream
state, and if the upstream was a skip, returns a synthetic
``LlmResponse`` that ADK treats as the model's final answer. No tokens
billed, no tool call.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest


def _ctx(state: dict | None = None):
    """Minimal ADK CallbackContext shape."""
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

    ctx = MagicMock()
    ctx.state = _State(state_dict)
    return ctx, state_dict


class TestSkipShortCircuitCallback:
    @pytest.mark.asyncio
    async def test_no_short_circuit_when_upstream_is_process(self):
        from apowerb.core.agent_helpers.callbacks import (
            build_skip_short_circuit_callback,
        )

        cb = build_skip_short_circuit_callback(
            upstream_key="ar_intake", downstream_output_key="ar_match"
        )
        ctx, _ = _ctx({"ar_intake": json.dumps({"status": "process"})})

        result = await cb(callback_context=ctx, llm_request=MagicMock())
        assert result is None  # no short-circuit; ADK continues to LLM

    @pytest.mark.asyncio
    async def test_short_circuit_when_upstream_missing(self):
        """When the upstream state key is absent (ar_intake not yet written),
        the callback MUST short-circuit downstream with a __skipped_upstream__
        sentinel instead of returning None and letting the LLM run.

        Rationale: returning None lets the downstream LLM run on empty context
        and improvise (emit tool calls, invent data). This caused incident
        2026-05-21 where the recorder produced ORDER_NOT_FOUND records on
        absent upstream. The defensive short-circuit is intentional.

        The original test expected None (no short-circuit) which was wrong.
        Updated 2026-05-23 to assert the correct defensive behaviour.
        """
        from apowerb.core.agent_helpers.callbacks import (
            build_skip_short_circuit_callback,
        )

        cb = build_skip_short_circuit_callback(
            upstream_key="ar_intake", downstream_output_key="ar_match"
        )
        ctx, state = _ctx({})  # upstream not set yet

        result = await cb(callback_context=ctx, llm_request=MagicMock())
        # Defensive short-circuit: upstream absent => same treatment as skip.
        # Returning None here would let the LLM improvise on empty input.
        assert result is not None, (
            "Upstream absent must short-circuit; returning None lets the LLM "
            "improvise on empty input (incident 2026-05-21)."
        )
        downstream = json.loads(state["ar_match"])
        assert downstream.get("__skipped_upstream__") is True
        assert "ar_intake" in downstream.get("reason", "")

    @pytest.mark.asyncio
    async def test_short_circuit_when_upstream_is_skip(self):
        from apowerb.core.agent_helpers.callbacks import (
            build_skip_short_circuit_callback,
        )

        cb = build_skip_short_circuit_callback(
            upstream_key="ar_intake", downstream_output_key="ar_match"
        )
        ctx, state = _ctx(
            {"ar_intake": json.dumps({"status": "skip", "raison": "x"})}
        )

        result = await cb(callback_context=ctx, llm_request=MagicMock())
        assert result is not None  # short-circuit
        # The downstream state slot is populated with a skip sentinel so
        # the next sub-agent's before_model_callback sees it and skips too.
        downstream = json.loads(state["ar_match"])
        assert downstream.get("__skipped_upstream__") is True
        assert "ar_intake" in downstream.get("reason", "")

    @pytest.mark.asyncio
    async def test_short_circuit_payload_is_valid_llm_response(self):
        """The returned LlmResponse must have content.parts with a text
        body so ADK's downstream after_agent_callback (and the agent's
        own output_key plumbing) sees a coherent final response."""
        from apowerb.core.agent_helpers.callbacks import (
            build_skip_short_circuit_callback,
        )

        cb = build_skip_short_circuit_callback(
            upstream_key="ar_intake", downstream_output_key="ar_match"
        )
        ctx, _ = _ctx({"ar_intake": json.dumps({"status": "skip"})})

        result = await cb(callback_context=ctx, llm_request=MagicMock())
        # Quack like LlmResponse: has .content, .content.parts, .parts[0].text
        assert hasattr(result, "content")
        assert result.content is not None
        assert result.content.parts
        text = result.content.parts[0].text
        payload = json.loads(text)
        assert payload.get("__skipped_upstream__") is True

    @pytest.mark.asyncio
    async def test_short_circuit_cascades_when_state_has_skip_sentinel(self):
        """A 3-hop cascade: if matcher already wrote a skip sentinel,
        recorder also short-circuits without re-checking the original
        upstream."""
        from apowerb.core.agent_helpers.callbacks import (
            build_skip_short_circuit_callback,
        )

        # Recorder reads ar_match, not ar_intake
        cb = build_skip_short_circuit_callback(
            upstream_key="ar_match", downstream_output_key="ar_record"
        )
        ctx, state = _ctx(
            {"ar_match": json.dumps({"__skipped_upstream__": True, "reason": "..."})}
        )

        result = await cb(callback_context=ctx, llm_request=MagicMock())
        assert result is not None
        downstream = json.loads(state["ar_record"])
        assert downstream.get("__skipped_upstream__") is True

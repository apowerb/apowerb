"""Audit fix, point 10 (partial): `tool_usage.score` used to ignore turns
and tokens entirely -- 40 turns and 200k tokens for 3 tool calls scored
100%. A configurable soft cap on tokens-per-call now discounts the score,
defaulting to 0 (disabled) so behaviour is unchanged until an operator
opts in -- the same "make it configurable, keep the default" pattern as
the per-judge pass thresholds (point 9).

The retry-vs-loop distinction the audit also raised (a retry after a
failed call should not count as a duplicate the way a repeat after a
success does) is NOT covered here: it requires reading each tool call's
own success/failure off `pulse_spans` (status_code/business_error,
already read by tool_execution_outcome.py) and is left as a documented
follow-up -- see the dev report.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apowerb.evaluation.evaluators.tool_usage import evaluate_tool_usage

_MODULE = "apowerb.evaluation.evaluators.tool_usage"


def _result(rows):
    result = MagicMock()
    result.fetchall.return_value = rows
    return result


def _span(span_id, name, attributes):
    return (span_id, name, attributes)


def _settings(soft_cap=0):
    return MagicMock(evaluation_tool_usage_tokens_per_call_soft_cap=soft_cap)


def _three_call_trace():
    return [
        _span("s1", "call_llm", {"gen_ai.usage.input_tokens": 60000, "gen_ai.usage.output_tokens": 10000}),
        _span("s2", "execute_tool tool_a", {"gcp.vertex.agent.tool_call_args": "{}"}),
        _span("s3", "execute_tool tool_b", {"gcp.vertex.agent.tool_call_args": "{}"}),
        _span("s4", "execute_tool tool_c", {"gcp.vertex.agent.tool_call_args": "{}"}),
    ]


@pytest.mark.asyncio
async def test_disabled_by_default_score_unaffected_by_heavy_token_use():
    db = AsyncMock()

    async def execute_side_effect(query, params=None):
        if "pulse_conversation_map" in str(query):
            return _result([("trace-1",)])
        return _result(_three_call_trace())

    db.execute.side_effect = execute_side_effect

    with patch(f"{_MODULE}.get_settings", return_value=_settings(soft_cap=0)):
        outcome = await evaluate_tool_usage(db, "session_heavy")

    assert outcome.score == 1.0


@pytest.mark.asyncio
async def test_soft_cap_discounts_the_score_for_disproportionate_token_use():
    """3 tool calls, 70000 tokens -> ~23333 tokens/call. A soft cap of
    5000 tokens/call means the call was ~4.7x over budget for the work
    it produced."""
    db = AsyncMock()

    async def execute_side_effect(query, params=None):
        if "pulse_conversation_map" in str(query):
            return _result([("trace-1",)])
        return _result(_three_call_trace())

    db.execute.side_effect = execute_side_effect

    with patch(f"{_MODULE}.get_settings", return_value=_settings(soft_cap=5000)):
        outcome = await evaluate_tool_usage(db, "session_heavy")

    assert outcome.score < 1.0
    assert outcome.score == pytest.approx(5000 / (70000 / 3), abs=1e-4)


@pytest.mark.asyncio
async def test_soft_cap_never_pushes_the_score_above_one():
    db = AsyncMock()
    light_trace = [
        _span("s1", "call_llm", {"gen_ai.usage.input_tokens": 100, "gen_ai.usage.output_tokens": 10}),
        _span("s2", "execute_tool tool_a", {"gcp.vertex.agent.tool_call_args": "{}"}),
    ]

    async def execute_side_effect(query, params=None):
        if "pulse_conversation_map" in str(query):
            return _result([("trace-1",)])
        return _result(light_trace)

    db.execute.side_effect = execute_side_effect

    with patch(f"{_MODULE}.get_settings", return_value=_settings(soft_cap=5000)):
        outcome = await evaluate_tool_usage(db, "session_light")

    assert outcome.score == 1.0


@pytest.mark.asyncio
async def test_resource_efficiency_is_visible_in_details():
    db = AsyncMock()

    async def execute_side_effect(query, params=None):
        if "pulse_conversation_map" in str(query):
            return _result([("trace-1",)])
        return _result(_three_call_trace())

    db.execute.side_effect = execute_side_effect

    with patch(f"{_MODULE}.get_settings", return_value=_settings(soft_cap=5000)):
        outcome = await evaluate_tool_usage(db, "session_heavy")

    assert outcome.details["tokens_per_call"] == pytest.approx(70000 / 3, abs=1e-4)
    assert outcome.details["tokens_per_call_soft_cap"] == 5000

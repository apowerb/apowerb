"""Unit tests for the deterministic tool-usage evaluator."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from apowerb.evaluation.evaluators.tool_usage import evaluate_tool_usage


def _result(rows):
    result = MagicMock()
    result.fetchall.return_value = rows
    return result


def _span(span_id, name, attributes):
    return (span_id, name, attributes)


@pytest.mark.asyncio
async def test_no_trace_mapped_is_not_applicable_rather_than_zero():
    db = AsyncMock()
    db.execute.return_value = _result([])

    outcome = await evaluate_tool_usage(db, "session_unknown")

    assert outcome.score is None
    assert outcome.passed is None
    assert outcome.applicable is False
    assert "no trace mapped" in outcome.details["not_applicable"]


@pytest.mark.asyncio
async def test_a_session_without_tool_calls_is_not_a_zero():
    db = AsyncMock()

    async def execute_side_effect(query, params=None):
        if "pulse_conversation_map" in str(query):
            return _result([("trace-1",)])
        return _result([])

    db.execute.side_effect = execute_side_effect

    outcome = await evaluate_tool_usage(db, "session_quiet")

    assert outcome.score is None
    assert outcome.applicable is False
    assert "no tool call" in outcome.details["not_applicable"]


@pytest.mark.asyncio
async def test_missing_telemetry_tables_are_a_missing_prerequisite():
    from sqlalchemy.exc import ProgrammingError

    db = AsyncMock()
    db.execute.side_effect = ProgrammingError("SELECT", {}, Exception("no table"))

    outcome = await evaluate_tool_usage(db, "session_untraced")

    assert outcome.applicable is False
    assert "th2pulse" in outcome.details["not_applicable"]
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_real_shaped_trace_no_duplicates(monkeypatch):
    """Mirrors the real trace of run_now_1201_1782986278_1786060801
    (agent1201, 2026-08-07): 5 call_llm turns, 5 execute_tool spans, two of
    them calling tool_text_to_sql with DIFFERENT args -- not an identical
    repeat.
    """
    db = AsyncMock()

    call_llm_rows = [
        _span("25bec7d45f994949", "call_llm", {"gen_ai.usage.input_tokens": 10909, "gen_ai.usage.output_tokens": 21}),
        _span("c61f895ccb593016", "call_llm", {"gen_ai.usage.input_tokens": 11001, "gen_ai.usage.output_tokens": 14}),
        _span("7d4f45a260c7d687", "call_llm", {"gen_ai.usage.input_tokens": 11896, "gen_ai.usage.output_tokens": 31}),
        _span("73d44627ca1d82b0", "call_llm", {"gen_ai.usage.input_tokens": 11995, "gen_ai.usage.output_tokens": 32}),
        _span("1edd07efddec0246", "call_llm", {"gen_ai.usage.input_tokens": 13424, "gen_ai.usage.output_tokens": 487}),
    ]
    execute_tool_rows = [
        _span("cde9644b8fb08184", "execute_tool tool_text_to_sql", {"gcp.vertex.agent.tool_call_args": '{"question": "test2"}'}),
        _span("b8b7f70b0c35ec4b", "execute_tool tool_get_database_schema", {"gcp.vertex.agent.tool_call_args": "{}"}),
        _span("f9187022a0620e3c", "execute_tool tool_run_sql", {"gcp.vertex.agent.tool_call_args": '{"sql": "SELECT * FROM th2demo.ventes LIMIT 10;"}'}),
        _span("3907d9de4075855c", "execute_tool tool_text_to_sql", {"gcp.vertex.agent.tool_call_args": '{"question": "Show me the first 10 rows of the ventes table."}'}),
        _span("a29f321d9b946c19", "execute_tool request_user_input", {"gcp.vertex.agent.tool_call_args": '{"question": "next?"}'}),
    ]

    async def execute_side_effect(query, params=None):
        if "pulse_conversation_map" in str(query):
            return _result([("3bed8e80ea770fde0717ce12aa7bf646",)])
        return _result(call_llm_rows + execute_tool_rows)

    db.execute.side_effect = execute_side_effect

    outcome = await evaluate_tool_usage(db, "run_now_1201_1782986278_1786060801")

    assert outcome.details["tool_calls"] == 5
    assert outcome.details["duplicate_calls"] == 0
    assert outcome.details["turns"] == 5
    assert outcome.details["input_tokens"] == 59225
    assert outcome.details["output_tokens"] == 585
    assert outcome.details["total_tokens"] == 59810
    assert outcome.score == 1.0
    assert outcome.passed is True


@pytest.mark.asyncio
async def test_identical_repeated_calls_are_penalized():
    db = AsyncMock()

    execute_tool_rows = [
        _span("s1", "execute_tool tool_run_sql", {"gcp.vertex.agent.tool_call_args": '{"sql": "SELECT 1"}'}),
        _span("s2", "execute_tool tool_run_sql", {"gcp.vertex.agent.tool_call_args": '{"sql": "SELECT 1"}'}),
        _span("s3", "execute_tool tool_run_sql", {"gcp.vertex.agent.tool_call_args": '{"sql": "SELECT 2"}'}),
    ]

    async def execute_side_effect(query, params=None):
        if "pulse_conversation_map" in str(query):
            return _result([("trace-x",)])
        return _result(execute_tool_rows)

    db.execute.side_effect = execute_side_effect

    outcome = await evaluate_tool_usage(db, "session_loop")

    assert outcome.details["tool_calls"] == 3
    assert outcome.details["duplicate_calls"] == 1
    assert outcome.details["duplicate_groups"] == [
        {"tool": "tool_run_sql", "args": '{"sql": "SELECT 1"}', "count": 2}
    ]
    assert outcome.score == pytest.approx(1.0 - 1 / 3, abs=1e-4)
    assert outcome.passed is False

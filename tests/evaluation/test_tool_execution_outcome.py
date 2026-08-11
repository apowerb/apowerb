"""Unit tests for the deterministic tool-execution-outcome evaluator."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from apowerb.evaluation.evaluators.tool_execution_outcome import (
    evaluate_tool_execution_outcome,
)


def _result(rows):
    result = MagicMock()
    result.fetchall.return_value = rows
    return result


@pytest.mark.asyncio
async def test_no_trace_mapped_is_not_applicable_rather_than_zero():
    """A session nobody instrumented is not a session that failed.

    Scored 0.0, it would sit in an average next to an agent that failed
    every tool call, and no reader of that average could tell them apart.
    """
    db = AsyncMock()
    db.execute.return_value = _result([])

    outcome = await evaluate_tool_execution_outcome(db, "session_unknown")

    assert outcome.score is None
    assert outcome.passed is None
    assert outcome.applicable is False
    assert "no trace mapped" in outcome.details["not_applicable"]


@pytest.mark.asyncio
async def test_a_session_without_tool_calls_is_not_a_total_failure():
    """Traces exist, but the agent simply never called a tool."""
    db = AsyncMock()

    async def execute_side_effect(query, params=None):
        if "pulse_conversation_map" in str(query):
            return _result([("trace-1",)])
        return _result([])

    db.execute.side_effect = execute_side_effect

    outcome = await evaluate_tool_execution_outcome(db, "session_quiet")

    assert outcome.score is None
    assert outcome.applicable is False
    assert "no tool call" in outcome.details["not_applicable"]


@pytest.mark.asyncio
async def test_missing_telemetry_tables_are_a_missing_prerequisite():
    """th2pulse is optional: an install without it must not get a 500."""
    from sqlalchemy.exc import ProgrammingError

    db = AsyncMock()
    db.execute.side_effect = ProgrammingError("SELECT", {}, Exception("no table"))

    outcome = await evaluate_tool_execution_outcome(db, "session_untraced")

    assert outcome.applicable is False
    assert "th2pulse" in outcome.details["not_applicable"]
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_business_failure_invisible_to_status_code_is_caught():
    """Mirrors a real DEV row (2026-08-06, session_1786030573591,
    agent basic_agent/1234, tool `tool_pdf_to_images`): `status_code` is
    UNSET even though the tool's own response encodes a failure. A naive
    evaluator trusting `status_code` alone must not see 100% success here.
    """
    db = AsyncMock()

    async def execute_side_effect(query, params=None):
        if "pulse_conversation_map" in str(query):
            return _result([("trace123",)])
        return _result(
            [
                (
                    "span1",
                    "execute_tool tool_pdf_to_images",
                    "",  # UNSET, verified against real DEV data
                    True,
                    {
                        "gcp.vertex.agent.tool_response": (
                            '{"status": "error", "message": "File not found"}'
                        )
                    },
                ),
                (
                    "span2",
                    "execute_tool create_downloadable_file",
                    "",
                    False,
                    {"gcp.vertex.agent.tool_response": '{"status": "ok"}'},
                ),
            ]
        )

    db.execute.side_effect = execute_side_effect

    outcome = await evaluate_tool_execution_outcome(db, "session_1786030573591")

    assert outcome.details["tool_calls"] == 2
    assert outcome.details["real_failures"] == 1
    assert outcome.score == 0.5
    assert outcome.passed is False
    # A status_code-only evaluator would have missed the failure entirely.
    assert outcome.details["naive_failures_from_status_code"] == 0
    assert outcome.details["naive_success_rate"] == 1.0
    assert outcome.details["status_code_is_reliable"] is False


@pytest.mark.asyncio
async def test_falls_back_to_business_error_column_when_response_unparseable():
    db = AsyncMock()

    async def execute_side_effect(query, params=None):
        if "pulse_conversation_map" in str(query):
            return _result([("trace456",)])
        return _result(
            [
                (
                    "span1",
                    "execute_tool tool_run_sql",
                    "ERROR",
                    True,
                    {"gcp.vertex.agent.tool_response": "not-json"},
                )
            ]
        )

    db.execute.side_effect = execute_side_effect

    outcome = await evaluate_tool_execution_outcome(db, "session_x")

    assert outcome.details["real_failures"] == 1
    assert outcome.details["status_code_is_reliable"] is True


# ---------------------------------------------------------------------------
# criteria: ordered list of what the evaluator measured
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_details_carries_ordered_criteria():
    db = AsyncMock()

    async def execute_side_effect(query, params=None):
        if "pulse_conversation_map" in str(query):
            return _result([("trace123",)])
        return _result(
            [
                (
                    "span1",
                    "execute_tool tool_pdf_to_images",
                    "",
                    True,
                    {"gcp.vertex.agent.tool_response": '{"status": "error"}'},
                ),
                (
                    "span2",
                    "execute_tool create_downloadable_file",
                    "",
                    False,
                    {"gcp.vertex.agent.tool_response": '{"status": "ok"}'},
                ),
            ]
        )

    db.execute.side_effect = execute_side_effect

    outcome = await evaluate_tool_execution_outcome(db, "session_1786030573591")

    assert outcome.details["criteria"] == [
        {"name": "tool_calls", "value": 2, "kind": "count"},
        {"name": "real_failures", "value": 1, "kind": "count"},
        {"name": "status_code_is_reliable", "value": False, "kind": "flag"},
    ]

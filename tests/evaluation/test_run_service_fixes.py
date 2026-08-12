"""Audit fixes in run_service.py:

- point 1 (GRAVE): `judged_model` is read from `llm_usage.model` for the
  session being evaluated, not from the agent's CURRENT config -- agents
  change model over time, so the anti-self-judging guard was comparing the
  judge against a model that may never have produced this conversation.
  Falls back to the agent's config only when no usable `llm_usage` row
  exists, and records which source was used.
- point 4 (GRAVE): a judge failure that happened AFTER a real litellm call
  (malformed JSON, missing key, timeout with partial usage) must still be
  billed -- `_run_judge`/`_run_llm_judge` read `judge_usage`/`judge_model`
  off the caught exception, the same way `_record_judge_usage` reads them
  off a successful outcome.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apowerb.evaluation.evaluators.base import EvaluationOutcome
from apowerb.evaluation.evaluators.task_completion_judge import SameJudgeError
from apowerb.evaluation.run_service import (
    SessionContext,
    resolve_session_context,
    run_and_persist,
)

def _transcript_rows():
    """`run_and_persist` now reads the transcript ONCE and hands it to every
    judge, so the shared session is queried here rather than inside each
    judge. Shape is ADK's real `events.event_data`, the one
    `extract_transcript` parses."""
    result = MagicMock()
    result.fetchall.return_value = [
        ({"content": {"role": "user", "parts": [{"text": "hello"}]}},),
        ({"content": {"role": "model", "parts": [{"text": "hi"}]}},),
    ]
    return result




def _user(email="me@example.com", role="user"):
    u = MagicMock()
    u.email = email
    u.role = role
    return u


def _session_row(app_name, user_id):
    result = MagicMock()
    result.first.return_value = (app_name, user_id)
    return result


def _agent_row(agent_id, owner_id, agent_model="gemini/gemini-2.5-flash"):
    row = MagicMock()
    row._asdict.return_value = {
        "agent_id": agent_id, "owner_id": owner_id, "agent_model": agent_model,
    }
    return [row]


def _llm_usage_row(model):
    result = MagicMock()
    result.first.return_value = (model,)
    return result


def _no_llm_usage_row():
    result = MagicMock()
    result.first.return_value = None
    return result


# ---------------------------------------------------------------------------
# point 1: judged_model sourced from llm_usage
# ---------------------------------------------------------------------------


class TestJudgedModelSourcing:
    @pytest.mark.asyncio
    async def test_judged_model_comes_from_llm_usage_when_available(self):
        """The agent is currently configured with a DIFFERENT model than
        the one that produced this session -- exactly the drift the audit
        measured on agent 1222 (gemini-2.5-flash -> flash-lite)."""
        db = AsyncMock()
        db.execute.side_effect = [
            _session_row("agent1234", "me@example.com"),
            _llm_usage_row("gemini/gemini-2.5-flash-lite"),
        ]

        with patch("apowerb.core.agent_main.agent_store") as store:
            store.get_list_agents.return_value = _agent_row(
                1234, "me@example.com", agent_model="gemini/gemini-2.5-flash"
            )
            ctx = await resolve_session_context(db, "session_x", _user())

        assert ctx.judged_model == "gemini/gemini-2.5-flash-lite"
        assert ctx.judged_model_source == "llm_usage"

    @pytest.mark.asyncio
    async def test_falls_back_to_agent_config_without_an_llm_usage_row(self):
        db = AsyncMock()
        db.execute.side_effect = [
            _session_row("agent1234", "me@example.com"),
            _no_llm_usage_row(),
        ]

        with patch("apowerb.core.agent_main.agent_store") as store:
            store.get_list_agents.return_value = _agent_row(
                1234, "me@example.com", agent_model="gemini/gemini-2.5-flash"
            )
            ctx = await resolve_session_context(db, "session_x", _user())

        assert ctx.judged_model == "gemini/gemini-2.5-flash"
        assert ctx.judged_model_source == "agent_config_fallback"

    @pytest.mark.asyncio
    async def test_llm_usage_query_excludes_the_evaluator_s_own_rows(self):
        """`_record_judge_usage` writes its OWN rows to `llm_usage` with
        `session_id` equal to the chat session it just judged
        (invocation_source='evaluation'). A second evaluation run on the
        same session must not pick up the judge's model as if it were the
        judged model -- verified against real DEV data (session
        session_1786432883708): the judge's gemini-2.5-pro rows sort after
        the chat's gemini-3-flash-preview rows by created_at."""
        db = AsyncMock()
        db.execute.side_effect = [
            _session_row("agent1234", "me@example.com"),
            _no_llm_usage_row(),
        ]

        with patch("apowerb.core.agent_main.agent_store") as store:
            store.get_list_agents.return_value = _agent_row(1234, "me@example.com")
            await resolve_session_context(db, "session_x", _user())

        judged_model_query = db.execute.call_args_list[1].args[0]
        query_sql = str(judged_model_query)
        assert "evaluation" in query_sql
        assert "llm_usage" in query_sql

    @pytest.mark.asyncio
    async def test_llm_usage_query_orders_by_most_recent(self):
        db = AsyncMock()
        db.execute.side_effect = [
            _session_row("agent1234", "me@example.com"),
            _no_llm_usage_row(),
        ]

        with patch("apowerb.core.agent_main.agent_store") as store:
            store.get_list_agents.return_value = _agent_row(1234, "me@example.com")
            await resolve_session_context(db, "session_x", _user())

        judged_model_query = db.execute.call_args_list[1].args[0]
        query_sql = str(judged_model_query).lower()
        assert "order by created_at desc" in query_sql


class TestJudgedModelSourcePropagatesToJudgeDetails:
    @pytest.mark.asyncio
    async def test_llm_usage_sourced_model_is_recorded_in_outcome_details(self):
        db = MagicMock()
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        db.execute = AsyncMock(return_value=_transcript_rows())
        ctx = SessionContext(
            agent_id=1234, app_name="agent1234", session_user_id="me@example.com",
            judged_model="gemini/gemini-2.5-flash-lite", judged_model_source="llm_usage",
            owner_id="me@example.com",
        )
        outcome = EvaluationOutcome(
            evaluator="task_completion_judge", kind="llm_judge", score=0.8, passed=True,
            details={"judged_model": "gemini/gemini-2.5-flash-lite"},
        )

        with patch(
            "apowerb.evaluation.run_service.evaluate_task_completion",
            new=AsyncMock(return_value=outcome),
        ):
            rows = await run_and_persist(db, ctx, "session_x", ["task_completion_judge"])

        assert rows[0].details["judged_model_source"] == "llm_usage"

    @pytest.mark.asyncio
    async def test_fallback_sourced_model_is_recorded_in_outcome_details(self):
        db = MagicMock()
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        db.execute = AsyncMock(return_value=_transcript_rows())
        ctx = SessionContext(
            agent_id=1234, app_name="agent1234", session_user_id="me@example.com",
            judged_model="gemini/gemini-2.5-flash", judged_model_source="agent_config_fallback",
            owner_id="me@example.com",
        )
        outcome = EvaluationOutcome(
            evaluator="task_completion_judge", kind="llm_judge", score=0.8, passed=True,
            details={"judged_model": "gemini/gemini-2.5-flash"},
        )

        with patch(
            "apowerb.evaluation.run_service.evaluate_task_completion",
            new=AsyncMock(return_value=outcome),
        ):
            rows = await run_and_persist(db, ctx, "session_x", ["task_completion_judge"])

        assert rows[0].details["judged_model_source"] == "agent_config_fallback"


# ---------------------------------------------------------------------------
# point 4: billing survives a post-call judge failure
# ---------------------------------------------------------------------------


def _db():
    db = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.execute = AsyncMock(return_value=_transcript_rows())
    return db


def _ctx():
    return SessionContext(
        agent_id=1234, app_name="agent1234", session_user_id="me@example.com",
        judged_model="gemini/gemini-2.5-flash", owner_id="me@example.com",
    )


def _usage_rows_added(db):
    return [
        call.args[0] for call in db.add.call_args_list
        if type(call.args[0]).__name__ == "LlmUsage"
    ]


def _failure_with_billing(exc_type, message):
    exc = exc_type(message)
    exc.judge_usage = {
        "input_tokens": 5000, "output_tokens": 50, "thoughts_tokens": 900,
        "cached_tokens": 0, "total_tokens": 5050,
    }
    exc.judge_model = "gemini/gemini-2.5-pro"
    exc.judge_is_byom = False
    return exc


class TestBillingSurvivesTaskCompletionJudgeFailure:
    @pytest.mark.asyncio
    async def test_missing_key_failure_is_billed(self):
        db = _db()
        with patch(
            "apowerb.evaluation.run_service.evaluate_task_completion",
            new=AsyncMock(
                side_effect=_failure_with_billing(
                    ValueError, "judge response missing required key(s): ['intent_resolution']"
                )
            ),
        ):
            rows = await run_and_persist(db, _ctx(), "session_x", ["task_completion_judge"])

        assert rows[0].score is None
        usage_rows = _usage_rows_added(db)
        assert len(usage_rows) == 1
        assert usage_rows[0].total_tokens == 5050
        assert usage_rows[0].model == "gemini/gemini-2.5-pro"

    @pytest.mark.asyncio
    async def test_no_content_runtime_error_is_billed(self):
        """The `RuntimeError` for an exhausted reasoning budget is caught
        by the FIRST except clause (`SameJudgeError, RuntimeError`), not
        the generic one -- billing must survive there too."""
        db = _db()
        with patch(
            "apowerb.evaluation.run_service.evaluate_task_completion",
            new=AsyncMock(
                side_effect=_failure_with_billing(
                    RuntimeError, "the judge returned no content"
                )
            ),
        ):
            rows = await run_and_persist(db, _ctx(), "session_x", ["task_completion_judge"])

        assert rows[0].score is None
        usage_rows = _usage_rows_added(db)
        assert len(usage_rows) == 1
        assert usage_rows[0].total_tokens == 5050

    @pytest.mark.asyncio
    async def test_failure_without_billing_attribute_writes_no_usage_row(self):
        """Config-not-set / self-judge refusal / connection errors never
        reached litellm -- no attribute, no row, same as before."""
        db = _db()
        with patch(
            "apowerb.evaluation.run_service.evaluate_task_completion",
            new=AsyncMock(side_effect=SameJudgeError("same model")),
        ):
            rows = await run_and_persist(db, _ctx(), "session_x", ["task_completion_judge"])

        assert rows[0].score is None
        assert _usage_rows_added(db) == []


class TestBillingSurvivesTheThreeOtherJudges:
    @pytest.mark.asyncio
    async def test_coherence_failure_is_billed(self):
        db = _db()
        with patch(
            "apowerb.evaluation.run_service.evaluate_coherence",
            new=AsyncMock(
                side_effect=_failure_with_billing(
                    ValueError, "judge response missing required key(s): ['coherence']"
                )
            ),
        ):
            rows = await run_and_persist(db, _ctx(), "session_x", ["coherence"])

        assert rows[0].score is None
        usage_rows = _usage_rows_added(db)
        assert len(usage_rows) == 1
        assert usage_rows[0].total_tokens == 5050

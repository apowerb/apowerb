"""Unit tests for evaluation/run_service.py.

Covers the two hard rules from the API contract:
- ownership is enforced at the entry of the request (resolve_session_context,
  owned_agent_ids), never by filtering an already-built list;
- a judge failure (not configured, same-model guard, or any other error)
  must turn into a non-applicable outcome, never an exception that would
  also take the deterministic evaluator's result down with it.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from apowerb.evaluation.evaluators.base import EvaluationOutcome
from apowerb.evaluation.evaluators.task_completion_judge import SameJudgeError
from apowerb.evaluation.run_service import (
    owned_agent_ids,
    resolve_session_context,
    run_and_persist,
)


def _user(email="owner@example.com", role="user"):
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
        "agent_id": agent_id,
        "owner_id": owner_id,
        "agent_model": agent_model,
    }
    return [row]


# ---------------------------------------------------------------------------
# resolve_session_context
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_session_id_is_404():
    db = AsyncMock()
    db.execute.return_value = MagicMock(first=MagicMock(return_value=None))

    with pytest.raises(HTTPException) as exc:
        await resolve_session_context(db, "session_nope", _user())

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_session_whose_agent_cannot_be_resolved_is_404():
    db = AsyncMock()
    db.execute.return_value = _session_row("superagent42", "owner@example.com")

    with pytest.raises(HTTPException) as exc:
        await resolve_session_context(db, "session_x", _user())

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_agent_not_in_store_is_404():
    db = AsyncMock()
    db.execute.return_value = _session_row("agent1234", "owner@example.com")

    with patch("apowerb.core.agent_main.agent_store") as store:
        store.get_list_agents.return_value = []
        with pytest.raises(HTTPException) as exc:
            await resolve_session_context(db, "session_x", _user())

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_session_owned_by_someone_else_is_403_for_a_regular_user():
    db = AsyncMock()
    db.execute.return_value = _session_row("agent1234", "someone-else@example.com")

    with patch("apowerb.core.agent_main.agent_store") as store:
        store.get_list_agents.return_value = _agent_row(1234, "someone-else@example.com")
        with pytest.raises(HTTPException) as exc:
            await resolve_session_context(db, "session_x", _user(email="me@example.com"))

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_admin_bypasses_ownership():
    db = AsyncMock()
    db.execute.return_value = _session_row("agent1234", "someone-else@example.com")

    with patch("apowerb.core.agent_main.agent_store") as store:
        store.get_list_agents.return_value = _agent_row(1234, "someone-else@example.com")
        ctx = await resolve_session_context(
            db, "session_x", _user(email="admin@example.com", role="admin")
        )

    assert ctx.agent_id == 1234


@pytest.mark.asyncio
async def test_owner_resolves_context_with_judged_model():
    db = AsyncMock()
    db.execute.return_value = _session_row("agent1234", "me@example.com")

    with patch("apowerb.core.agent_main.agent_store") as store:
        store.get_list_agents.return_value = _agent_row(
            1234, "me@example.com", agent_model="openai/Mistral-Small-3.2-24B"
        )
        ctx = await resolve_session_context(db, "session_x", _user(email="me@example.com"))

    assert ctx.agent_id == 1234
    assert ctx.app_name == "agent1234"
    assert ctx.judged_model == "openai/Mistral-Small-3.2-24B"


# ---------------------------------------------------------------------------
# owned_agent_ids
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_is_unrestricted():
    db = AsyncMock()
    result = await owned_agent_ids(db, _user(role="admin"))
    assert result is None


@pytest.mark.asyncio
async def test_regular_user_gets_exactly_their_agent_ids():
    db = AsyncMock()

    with patch("apowerb.core.agent_main.agent_store") as store:
        store.get_list_agents.return_value = [(1,), (2,)]
        result = await owned_agent_ids(db, _user(email="me@example.com"))

    assert result == {1, 2}


# ---------------------------------------------------------------------------
# run_and_persist
# ---------------------------------------------------------------------------


def _db():
    """`AsyncSession.add()` is a plain sync method; unlike `execute`/`commit`/
    `refresh`, wrapping it in `AsyncMock` would make it return an unawaited
    coroutine and warn. Match the real API shape instead."""
    db = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    return db


def _ctx(agent_id=1234, app_name="agent1234", session_user_id="me@example.com",
         judged_model="gemini/gemini-2.5-flash"):
    from apowerb.evaluation.run_service import SessionContext

    return SessionContext(
        agent_id=agent_id,
        app_name=app_name,
        session_user_id=session_user_id,
        judged_model=judged_model,
        owner_id=session_user_id,
    )


@pytest.mark.asyncio
async def test_unknown_evaluator_name_is_400():
    db = _db()

    with pytest.raises(HTTPException) as exc:
        await run_and_persist(db, _ctx(), "session_x", ["not_a_real_evaluator"])

    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_default_runs_both_evaluators_and_persists_both():
    db = _db()
    deterministic = EvaluationOutcome(
        evaluator="tool_execution_outcome", kind="deterministic", score=1.0, passed=True
    )
    judged = EvaluationOutcome(
        evaluator="task_completion_judge", kind="llm_judge", score=0.8, passed=True
    )

    with (
        patch(
            "apowerb.evaluation.run_service.evaluate_tool_execution_outcome",
            new=AsyncMock(return_value=deterministic),
        ),
        patch(
            "apowerb.evaluation.run_service.evaluate_task_completion",
            new=AsyncMock(return_value=judged),
        ),
    ):
        rows = await run_and_persist(db, _ctx(), "session_x", None)

    assert len(rows) == 2
    assert db.add.call_count == 2
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_judge_same_model_error_becomes_not_applicable_not_an_exception():
    db = _db()
    deterministic = EvaluationOutcome(
        evaluator="tool_execution_outcome", kind="deterministic", score=1.0, passed=True
    )

    with (
        patch(
            "apowerb.evaluation.run_service.evaluate_tool_execution_outcome",
            new=AsyncMock(return_value=deterministic),
        ),
        patch(
            "apowerb.evaluation.run_service.evaluate_task_completion",
            new=AsyncMock(side_effect=SameJudgeError("same model")),
        ),
    ):
        rows = await run_and_persist(db, _ctx(), "session_x", None)

    assert len(rows) == 2
    judge_row = [r for r in rows if r.evaluator_name == "task_completion_judge"][0]
    assert judge_row.score is None
    assert judge_row.passed is None
    assert "same model" in judge_row.details["not_applicable"]
    det_row = [r for r in rows if r.evaluator_name == "tool_execution_outcome"][0]
    assert det_row.score == 1.0


@pytest.mark.asyncio
async def test_judge_not_configured_becomes_not_applicable():
    db = _db()
    deterministic = EvaluationOutcome(
        evaluator="tool_execution_outcome", kind="deterministic", score=1.0, passed=True
    )

    with (
        patch(
            "apowerb.evaluation.run_service.evaluate_tool_execution_outcome",
            new=AsyncMock(return_value=deterministic),
        ),
        patch(
            "apowerb.evaluation.run_service.evaluate_task_completion",
            new=AsyncMock(side_effect=RuntimeError("EVALUATION_JUDGE_MODEL is not configured")),
        ),
    ):
        rows = await run_and_persist(db, _ctx(), "session_x", None)

    judge_row = [r for r in rows if r.evaluator_name == "task_completion_judge"][0]
    assert judge_row.score is None
    assert "not configured" in judge_row.details["not_applicable"]


@pytest.mark.asyncio
async def test_unexpected_judge_failure_never_propagates():
    """A litellm/network failure must not raise -- it must not be able to
    take the deterministic evaluator's already-computed result down with it.
    """
    db = _db()
    deterministic = EvaluationOutcome(
        evaluator="tool_execution_outcome", kind="deterministic", score=0.5, passed=False
    )

    with (
        patch(
            "apowerb.evaluation.run_service.evaluate_tool_execution_outcome",
            new=AsyncMock(return_value=deterministic),
        ),
        patch(
            "apowerb.evaluation.run_service.evaluate_task_completion",
            new=AsyncMock(side_effect=TimeoutError("litellm timed out")),
        ),
    ):
        rows = await run_and_persist(db, _ctx(), "session_x", None)

    assert len(rows) == 2
    judge_row = [r for r in rows if r.evaluator_name == "task_completion_judge"][0]
    assert judge_row.score is None
    assert judge_row.passed is None
    assert "litellm timed out" in judge_row.details["not_applicable"]


@pytest.mark.asyncio
async def test_requesting_only_the_deterministic_evaluator_skips_the_judge():
    db = _db()
    deterministic = EvaluationOutcome(
        evaluator="tool_execution_outcome", kind="deterministic", score=1.0, passed=True
    )

    with (
        patch(
            "apowerb.evaluation.run_service.evaluate_tool_execution_outcome",
            new=AsyncMock(return_value=deterministic),
        ),
        patch(
            "apowerb.evaluation.run_service.evaluate_task_completion",
            new=AsyncMock(),
        ) as judge_mock,
    ):
        rows = await run_and_persist(db, _ctx(), "session_x", ["tool_execution_outcome"])

    assert len(rows) == 1
    judge_mock.assert_not_called()

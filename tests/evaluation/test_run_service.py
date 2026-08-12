"""Unit tests for evaluation/run_service.py.

Covers the two hard rules from the API contract:
- ownership is enforced at the entry of the request (resolve_session_context,
  owned_agent_ids), never by filtering an already-built list;
- a judge failure (not configured, same-model guard, or any other error)
  must turn into a non-applicable outcome, never an exception that would
  also take the deterministic evaluator's result down with it.
"""

import uuid

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


def _no_llm_usage_row():
    """`resolve_session_context` queries `llm_usage` for the judged_model
    after the session/agent lookup -- an empty result exercises the
    agent-config fallback, the same outcome these ownership-focused tests
    asserted before that second query existed."""
    result = MagicMock()
    result.first.return_value = None
    return result


def _llm_usage_row(model):
    result = MagicMock()
    result.first.return_value = (model,)
    return result


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
    db.execute.side_effect = [
        _session_row("agent1234", "someone-else@example.com"),
        _no_llm_usage_row(),
    ]

    with patch("apowerb.core.agent_main.agent_store") as store:
        store.get_list_agents.return_value = _agent_row(1234, "someone-else@example.com")
        ctx = await resolve_session_context(
            db, "session_x", _user(email="admin@example.com", role="admin")
        )

    assert ctx.agent_id == 1234


@pytest.mark.asyncio
async def test_owner_resolves_context_with_judged_model():
    """No llm_usage row for this session: falls back to the agent's
    current config, as before point 1's fix."""
    db = AsyncMock()
    db.execute.side_effect = [
        _session_row("agent1234", "me@example.com"),
        _no_llm_usage_row(),
    ]

    with patch("apowerb.core.agent_main.agent_store") as store:
        store.get_list_agents.return_value = _agent_row(
            1234, "me@example.com", agent_model="openai/Mistral-Small-3.2-24B"
        )
        ctx = await resolve_session_context(db, "session_x", _user(email="me@example.com"))

    assert ctx.agent_id == 1234
    assert ctx.app_name == "agent1234"
    assert ctx.judged_model == "openai/Mistral-Small-3.2-24B"
    assert ctx.judged_model_source == "agent_config_fallback"


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


def _db():
    """`AsyncSession.add()` is a plain sync method; unlike `execute`/`commit`/
    `refresh`, wrapping it in `AsyncMock` would make it return an unawaited
    coroutine and warn. Match the real API shape instead."""
    db = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.execute = AsyncMock(return_value=_transcript_rows())
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
        # Explicit list, not None: KNOWN_EVALUATORS now has six entries
        # (tool_usage/coherence/completeness/hallucination were added), so
        # the default (evaluators=None) runs all of them -- this test is
        # about these two specific evaluators running and persisting
        # correctly together, not about what the current full default set is.
        rows = await run_and_persist(
            db, _ctx(), "session_x", ["tool_execution_outcome", "task_completion_judge"]
        )

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
        # Explicit list: KNOWN_EVALUATORS now has six entries, this test
        # is about tool_execution_outcome + task_completion_judge specifically.
        rows = await run_and_persist(
            db, _ctx(), "session_x", ["tool_execution_outcome", "task_completion_judge"]
        )

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
        # Explicit list: KNOWN_EVALUATORS now has six entries, this test
        # is about tool_execution_outcome + task_completion_judge specifically.
        rows = await run_and_persist(
            db, _ctx(), "session_x", ["tool_execution_outcome", "task_completion_judge"]
        )

    judge_row = [r for r in rows if r.evaluator_name == "task_completion_judge"][0]
    assert judge_row.score is None
    assert "not configured" in judge_row.details["not_applicable"]
    # An unconfigured judge has no real model string to record -- storing
    # "" (settings' default) would be indistinguishable from a genuinely
    # blank-but-set value.
    assert judge_row.judge_model is None


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
        # Explicit list: KNOWN_EVALUATORS now has six entries, this test
        # is about tool_execution_outcome + task_completion_judge specifically.
        rows = await run_and_persist(
            db, _ctx(), "session_x", ["tool_execution_outcome", "task_completion_judge"]
        )

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


# ---------------------------------------------------------------------------
# EVALUATOR_REGISTRY / list_evaluator_specs
# ---------------------------------------------------------------------------


def test_evaluator_registry_describes_every_known_evaluator():
    """The front builds its checkboxes from this, so a wrong `kind` or a
    wrong `requires_judge` is a wrong screen — an evaluator offered when no
    judge can run it, or greyed out when it needs none."""
    from apowerb.evaluation.run_service import list_evaluator_specs

    specs = {spec.name: spec for spec in list_evaluator_specs()}

    expected = {
        "tool_execution_outcome": ("deterministic", False),
        "tool_usage": ("deterministic", False),
        "task_completion_judge": ("llm_judge", True),
        "coherence": ("llm_judge", True),
        "completeness": ("llm_judge", True),
        "hallucination": ("llm_judge", True),
    }

    assert set(specs) == set(expected)
    for name, (kind, requires_judge) in expected.items():
        assert specs[name].kind == kind, name
        assert specs[name].requires_judge is requires_judge, name


def test_known_evaluators_is_derived_from_the_registry():
    from apowerb.evaluation.run_service import EVALUATOR_REGISTRY, KNOWN_EVALUATORS

    assert set(KNOWN_EVALUATORS) == {spec.name for spec in EVALUATOR_REGISTRY}


# ---------------------------------------------------------------------------
# resolve_session_context: agent_name
# run_and_persist -- the four new evaluators (tool_usage, coherence,
# completeness, hallucination)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_session_context_carries_agent_name():
    db = AsyncMock()
    db.execute.side_effect = [
        _session_row("agent1234", "me@example.com"),
        _no_llm_usage_row(),
    ]

    with patch("apowerb.core.agent_main.agent_store") as store:
        row = MagicMock()
        row._asdict.return_value = {
            "agent_id": 1234,
            "owner_id": "me@example.com",
            "agent_model": "gemini/gemini-2.5-flash",
            "agent_name": "Analyste AR",
        }
        store.get_list_agents.return_value = [row]
        ctx = await resolve_session_context(db, "session_x", _user(email="me@example.com"))

    assert ctx.agent_name == "Analyste AR"


@pytest.mark.asyncio
async def test_resolve_session_context_falls_back_to_app_name_without_agent_name():
    db = AsyncMock()
    db.execute.side_effect = [
        _session_row("agent1234", "me@example.com"),
        _no_llm_usage_row(),
    ]

    with patch("apowerb.core.agent_main.agent_store") as store:
        store.get_list_agents.return_value = _agent_row(1234, "me@example.com")
        ctx = await resolve_session_context(db, "session_x", _user(email="me@example.com"))

    assert ctx.agent_name == "agent1234"


# ---------------------------------------------------------------------------
# run_and_persist: BYOM threading
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_and_persist_threads_byom_judge_model_and_key_to_the_judge():
    db = _db()
    judged = EvaluationOutcome(
        evaluator="task_completion_judge",
        kind="llm_judge",
        score=0.8,
        passed=True,
        details={"judge_model": "anthropic/claude-3-5-sonnet", "judge_is_byom": True},
    )

    with patch(
        "apowerb.evaluation.run_service.evaluate_task_completion",
        new=AsyncMock(return_value=judged),
    ) as judge_mock:
        await run_and_persist(
            db, _ctx(), "session_x", ["task_completion_judge"],
            judge_model="anthropic/claude-3-5-sonnet",
            judge_api_key="byom-secret",
        )

    call_kwargs = judge_mock.call_args.kwargs
    assert call_kwargs["judge_model"] == "anthropic/claude-3-5-sonnet"
    assert call_kwargs["judge_api_key"] == "byom-secret"


@pytest.mark.asyncio
async def test_run_and_persist_stores_the_effective_judge_model_not_settings():
    """`EvaluationResult.judge_model` must reflect what the evaluator
    actually used (which may be a caller's BYOM model), not the server's
    configured default -- they can differ."""
    db = _db()
    judged = EvaluationOutcome(
        evaluator="task_completion_judge",
        kind="llm_judge",
        score=0.8,
        passed=True,
        details={"judge_model": "anthropic/claude-3-5-sonnet", "judge_is_byom": True},
    )

    with patch(
        "apowerb.evaluation.run_service.evaluate_task_completion",
        new=AsyncMock(return_value=judged),
    ):
        rows = await run_and_persist(
            db, _ctx(), "session_x", ["task_completion_judge"],
            judge_model="anthropic/claude-3-5-sonnet",
            judge_api_key="byom-secret",
        )

    assert rows[0].judge_model == "anthropic/claude-3-5-sonnet"


# ---------------------------------------------------------------------------
# run_and_persist: llm_usage accounting
# ---------------------------------------------------------------------------


def _judged_outcome(*, judge_is_byom, judge_model="gemini/gemini-2.5-flash", usage=None):
    return EvaluationOutcome(
        evaluator="task_completion_judge",
        kind="llm_judge",
        score=0.8,
        passed=True,
        details={
            "judge_model": judge_model,
            "judge_is_byom": judge_is_byom,
            "judge_usage": usage
            or {
                "input_tokens": 100,
                "output_tokens": 20,
                "thoughts_tokens": 5,
                "cached_tokens": 0,
                "total_tokens": 125,
            },
        },
    )


def _usage_rows_added(db):
    return [
        call.args[0] for call in db.add.call_args_list
        if type(call.args[0]).__name__ == "LlmUsage"
    ]


@pytest.mark.asyncio
async def test_server_judge_run_writes_llm_usage_billed_to_thaink2():
    db = _db()
    outcome = _judged_outcome(judge_is_byom=False)

    with patch(
        "apowerb.evaluation.run_service.evaluate_task_completion",
        new=AsyncMock(return_value=outcome),
    ):
        await run_and_persist(
            db, _ctx(agent_id=1234, app_name="agent1234"), "session_x",
            ["task_completion_judge"],
        )

    usage_rows = _usage_rows_added(db)
    assert len(usage_rows) == 1
    row = usage_rows[0]
    assert row.invocation_source == "evaluation"
    assert row.billed_to_thaink2 is True
    assert row.model == "gemini/gemini-2.5-flash"
    assert row.input_tokens == 100
    assert row.output_tokens == 20
    assert row.thoughts_tokens == 5
    assert row.total_tokens == 125
    assert row.agent_id == 1234


@pytest.mark.asyncio
async def test_byom_judge_run_writes_llm_usage_not_billed_to_thaink2():
    db = _db()
    outcome = _judged_outcome(judge_is_byom=True, judge_model="anthropic/claude-3-5-sonnet")

    with patch(
        "apowerb.evaluation.run_service.evaluate_task_completion",
        new=AsyncMock(return_value=outcome),
    ):
        await run_and_persist(
            db, _ctx(), "session_x", ["task_completion_judge"],
            judge_model="anthropic/claude-3-5-sonnet", judge_api_key="byom-secret",
        )

    usage_rows = _usage_rows_added(db)
    assert len(usage_rows) == 1
    assert usage_rows[0].billed_to_thaink2 is False
    assert usage_rows[0].model == "anthropic/claude-3-5-sonnet"


@pytest.mark.asyncio
async def test_not_applicable_judge_outcome_writes_no_llm_usage_row():
    db = _db()
    outcome = EvaluationOutcome.not_applicable(
        evaluator="task_completion_judge",
        kind="llm_judge",
        reason="the session has no transcript to judge",
        session_id="session_x",
    )

    with patch(
        "apowerb.evaluation.run_service.evaluate_task_completion",
        new=AsyncMock(return_value=outcome),
    ):
        await run_and_persist(db, _ctx(), "session_x", ["task_completion_judge"])

    assert _usage_rows_added(db) == []


@pytest.mark.asyncio
async def test_deterministic_evaluator_alone_writes_no_llm_usage_row():
    db = _db()
    deterministic = EvaluationOutcome(
        evaluator="tool_execution_outcome", kind="deterministic", score=1.0, passed=True
    )

    with patch(
        "apowerb.evaluation.run_service.evaluate_tool_execution_outcome",
        new=AsyncMock(return_value=deterministic),
    ):
        await run_and_persist(db, _ctx(), "session_x", ["tool_execution_outcome"])

    assert _usage_rows_added(db) == []


@pytest.mark.asyncio
async def test_llm_usage_write_failure_is_best_effort_and_does_not_raise(caplog):
    """A broken accounting write must never take the evaluation result
    down with it -- but it must be logged loudly."""
    db = _db()
    outcome = _judged_outcome(judge_is_byom=False)

    call_count = {"n": 0}

    async def _commit_side_effect():
        call_count["n"] += 1
        if call_count["n"] == 2:  # 1st commit persists eval rows, 2nd is usage
            raise RuntimeError("db is down")

    db.commit = AsyncMock(side_effect=_commit_side_effect)
    db.rollback = AsyncMock()

    with patch(
        "apowerb.evaluation.run_service.evaluate_task_completion",
        new=AsyncMock(return_value=outcome),
    ):
        with caplog.at_level("ERROR", logger="apowerb.evaluation.run_service"):
            rows = await run_and_persist(db, _ctx(), "session_x", ["task_completion_judge"])

    assert len(rows) == 1
    assert rows[0].score == 0.8
    db.rollback.assert_awaited_once()
    assert any("llm_usage" in r.message.lower() for r in caplog.records)


@pytest.mark.asyncio
async def test_eval_rows_are_refreshed_after_the_usage_write_commit():
    """A second `db.commit()` for the usage row (best-effort accounting)
    expires every object already loaded in the session by default -- if
    `db.refresh()` on the eval rows runs BEFORE that second commit, the
    rows come back from `run_and_persist` in an expired state, and the
    caller (the router, building the HTTP response) crashes reading
    `row.id` outside an awaited context. Real bug, found only against a
    real AsyncSession/DB -- unittest mocks don't model SQLAlchemy
    expiration, so this pins the call ORDER instead as a regression guard.
    """
    db = _db()
    outcome = _judged_outcome(judge_is_byom=False)
    calls: list[str] = []

    async def _commit_side_effect():
        calls.append("commit")

    async def _refresh_side_effect(row):
        calls.append("refresh")

    db.commit = AsyncMock(side_effect=_commit_side_effect)
    db.refresh = AsyncMock(side_effect=_refresh_side_effect)

    with patch(
        "apowerb.evaluation.run_service.evaluate_task_completion",
        new=AsyncMock(return_value=outcome),
    ):
        await run_and_persist(db, _ctx(), "session_x", ["task_completion_judge"])

    # commit(eval rows), commit(usage row), THEN refresh -- never before
    # the last commit.
    assert calls == ["commit", "commit", "refresh"]
async def test_the_four_new_evaluators_are_known_and_dispatched():
    db = _db()
    outcomes = {
        "tool_usage": EvaluationOutcome(
            evaluator="tool_usage", kind="deterministic", score=1.0, passed=True
        ),
        "coherence": EvaluationOutcome(
            evaluator="coherence", kind="llm_judge", score=0.9, passed=True
        ),
        "completeness": EvaluationOutcome(
            evaluator="completeness", kind="llm_judge", score=0.8, passed=True
        ),
        "hallucination": EvaluationOutcome(
            evaluator="hallucination",
            kind="llm_judge",
            score=0.95,
            passed=True,
            details={"grounding": "unavailable"},
        ),
    }

    with (
        patch(
            "apowerb.evaluation.run_service.evaluate_tool_usage",
            new=AsyncMock(return_value=outcomes["tool_usage"]),
        ),
        patch(
            "apowerb.evaluation.run_service.evaluate_coherence",
            new=AsyncMock(return_value=outcomes["coherence"]),
        ),
        patch(
            "apowerb.evaluation.run_service.evaluate_completeness",
            new=AsyncMock(return_value=outcomes["completeness"]),
        ),
        patch(
            "apowerb.evaluation.run_service.evaluate_hallucination",
            new=AsyncMock(return_value=outcomes["hallucination"]),
        ),
    ):
        rows = await run_and_persist(
            db,
            _ctx(),
            "session_x",
            ["tool_usage", "coherence", "completeness", "hallucination"],
        )

    assert len(rows) == 4
    names = {row.evaluator_name for row in rows}
    assert names == {"tool_usage", "coherence", "completeness", "hallucination"}
    hallucination_row = [r for r in rows if r.evaluator_name == "hallucination"][0]
    assert hallucination_row.details["grounding"] == "unavailable"


@pytest.mark.asyncio
async def test_a_new_llm_judge_failure_becomes_not_applicable_not_an_exception():
    """Same guarantee as task_completion_judge: SameJudgeError, not
    configured, or any other litellm failure must never propagate."""
    db = _db()

    with patch(
        "apowerb.evaluation.run_service.evaluate_coherence",
        new=AsyncMock(side_effect=SameJudgeError("same model")),
    ):
        rows = await run_and_persist(db, _ctx(), "session_x", ["coherence"])

    assert len(rows) == 1
    assert rows[0].score is None
    assert rows[0].passed is None
    assert "same model" in rows[0].details["not_applicable"]


@pytest.mark.asyncio
async def test_a_new_llm_judge_unexpected_failure_never_propagates():
    db = _db()

    with patch(
        "apowerb.evaluation.run_service.evaluate_hallucination",
        new=AsyncMock(side_effect=TimeoutError("litellm timed out")),
    ):
        rows = await run_and_persist(db, _ctx(), "session_x", ["hallucination"])

    assert len(rows) == 1
    assert rows[0].score is None
    assert "litellm timed out" in rows[0].details["not_applicable"]


# ---------------------------------------------------------------------------
# run_and_persist: run_id shared by every row of one call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_and_persist_stamps_every_row_with_the_same_run_id():
    db = _db()
    outcomes = {
        "tool_usage": EvaluationOutcome(
            evaluator="tool_usage", kind="deterministic", score=1.0, passed=True
        ),
        "coherence": EvaluationOutcome(
            evaluator="coherence", kind="llm_judge", score=0.9, passed=True
        ),
    }

    with (
        patch(
            "apowerb.evaluation.run_service.evaluate_tool_usage",
            new=AsyncMock(return_value=outcomes["tool_usage"]),
        ),
        patch(
            "apowerb.evaluation.run_service.evaluate_coherence",
            new=AsyncMock(return_value=outcomes["coherence"]),
        ),
    ):
        rows = await run_and_persist(db, _ctx(), "session_x", ["tool_usage", "coherence"])

    assert len(rows) == 2
    run_ids = {row.run_id for row in rows}
    assert len(run_ids) == 1
    assert isinstance(rows[0].run_id, uuid.UUID)


@pytest.mark.asyncio
async def test_run_and_persist_generates_a_new_run_id_when_two_calls_are_made():
    db = _db()
    outcome = EvaluationOutcome(
        evaluator="tool_usage", kind="deterministic", score=1.0, passed=True
    )

    with patch(
        "apowerb.evaluation.run_service.evaluate_tool_usage",
        new=AsyncMock(return_value=outcome),
    ):
        first = await run_and_persist(db, _ctx(), "session_x", ["tool_usage"])
        second = await run_and_persist(db, _ctx(), "session_x", ["tool_usage"])

    assert first[0].run_id != second[0].run_id


@pytest.mark.asyncio
async def test_run_and_persist_accepts_an_explicit_run_id():
    """The router generates the run_id once (it must appear at the root of
    the HTTP response even if the evaluator list is empty), and passes it
    in -- run_and_persist must use it rather than generating its own."""
    db = _db()
    outcome = EvaluationOutcome(
        evaluator="tool_usage", kind="deterministic", score=1.0, passed=True
    )
    forced_run_id = uuid.uuid4()

    with patch(
        "apowerb.evaluation.run_service.evaluate_tool_usage",
        new=AsyncMock(return_value=outcome),
    ):
        rows = await run_and_persist(
            db, _ctx(), "session_x", ["tool_usage"], run_id=forced_run_id
        )

    assert rows[0].run_id == forced_run_id


# ---------------------------------------------------------------------------
# run_and_persist: locale threading to the four LLM judges
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_and_persist_threads_locale_to_task_completion_judge():
    db = _db()
    judged = EvaluationOutcome(
        evaluator="task_completion_judge", kind="llm_judge", score=0.8, passed=True
    )

    with patch(
        "apowerb.evaluation.run_service.evaluate_task_completion",
        new=AsyncMock(return_value=judged),
    ) as judge_mock:
        await run_and_persist(
            db, _ctx(), "session_x", ["task_completion_judge"], locale="fr"
        )

    assert judge_mock.call_args.kwargs["locale"] == "fr"


@pytest.mark.asyncio
async def test_run_and_persist_threads_locale_to_the_three_new_judges():
    db = _db()
    outcomes = {
        "coherence": EvaluationOutcome(
            evaluator="coherence", kind="llm_judge", score=0.9, passed=True
        ),
        "completeness": EvaluationOutcome(
            evaluator="completeness", kind="llm_judge", score=0.8, passed=True
        ),
        "hallucination": EvaluationOutcome(
            evaluator="hallucination", kind="llm_judge", score=0.95, passed=True
        ),
    }

    with (
        patch(
            "apowerb.evaluation.run_service.evaluate_coherence",
            new=AsyncMock(return_value=outcomes["coherence"]),
        ) as coherence_mock,
        patch(
            "apowerb.evaluation.run_service.evaluate_completeness",
            new=AsyncMock(return_value=outcomes["completeness"]),
        ) as completeness_mock,
        patch(
            "apowerb.evaluation.run_service.evaluate_hallucination",
            new=AsyncMock(return_value=outcomes["hallucination"]),
        ) as hallucination_mock,
    ):
        await run_and_persist(
            db, _ctx(), "session_x",
            ["coherence", "completeness", "hallucination"],
            locale="fr",
        )

    assert coherence_mock.call_args.kwargs["locale"] == "fr"
    assert completeness_mock.call_args.kwargs["locale"] == "fr"
    assert hallucination_mock.call_args.kwargs["locale"] == "fr"


@pytest.mark.asyncio
async def test_omitted_locale_defaults_to_none_not_a_crash():
    db = _db()
    judged = EvaluationOutcome(
        evaluator="task_completion_judge", kind="llm_judge", score=0.8, passed=True
    )

    with patch(
        "apowerb.evaluation.run_service.evaluate_task_completion",
        new=AsyncMock(return_value=judged),
    ) as judge_mock:
        await run_and_persist(db, _ctx(), "session_x", ["task_completion_judge"])

    assert judge_mock.call_args.kwargs["locale"] is None


# ---------------------------------------------------------------------------
# The judges are the wall time of a run: one LLM call each. Awaited one after
# another, six evaluators took ~30s -- right on the frontend proxy's own
# limit, which failed runs the backend had in fact completed. These three
# tests hold the fix down.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_judges_run_concurrently_not_one_after_another():
    """A barrier of four can only be crossed if all four judges are in
    flight at the same time. Run them sequentially and the first one waits
    for three that will never arrive.

    Assert on the SCORES, not on the row count: `_run_llm_judge` swallows
    every exception by design, so a serial run still returns four rows --
    four non-applicable ones carrying the timeout as their reason. Counting
    rows here passes on the very implementation this test exists to reject.
    """
    import asyncio

    barrier = asyncio.Barrier(4)

    def _judge(name):
        async def _fn(db, **kwargs):
            # Every judge must reach this point before any may leave it.
            await asyncio.wait_for(barrier.wait(), timeout=2)
            return EvaluationOutcome(
                evaluator=name, kind="llm_judge", score=1.0, passed=True
            )

        return AsyncMock(side_effect=_fn)

    db = _db()
    with (
        patch("apowerb.evaluation.run_service.evaluate_task_completion",
              new=_judge("task_completion_judge")),
        patch("apowerb.evaluation.run_service.evaluate_coherence",
              new=_judge("coherence")),
        patch("apowerb.evaluation.run_service.evaluate_completeness",
              new=_judge("completeness")),
        patch("apowerb.evaluation.run_service.evaluate_hallucination",
              new=_judge("hallucination")),
    ):
        rows = await run_and_persist(
            db, _ctx(), "session_x",
            ["task_completion_judge", "coherence", "completeness", "hallucination"],
        )

    # Every judge got past the barrier, so every judge was running while the
    # others were. A serial run leaves these None.
    assert [row.score for row in rows] == [1.0, 1.0, 1.0, 1.0]


@pytest.mark.asyncio
async def test_the_transcript_is_read_once_for_every_judge():
    """Four judges used to issue four identical queries for the same
    transcript. One read is both three fewer queries and the reason the
    judges can be gathered at all: they are left with no database work on a
    session that permits no concurrent operation."""
    db = _db()
    outcome = EvaluationOutcome(evaluator="x", kind="llm_judge", score=1.0, passed=True)

    with (
        patch("apowerb.evaluation.run_service.evaluate_task_completion",
              new=AsyncMock(return_value=outcome)),
        patch("apowerb.evaluation.run_service.evaluate_coherence",
              new=AsyncMock(return_value=outcome)),
        patch("apowerb.evaluation.run_service.evaluate_completeness",
              new=AsyncMock(return_value=outcome)),
        patch("apowerb.evaluation.run_service.evaluate_hallucination",
              new=AsyncMock(return_value=outcome)) as hallucination_mock,
    ):
        await run_and_persist(
            db, _ctx(), "session_x",
            ["task_completion_judge", "coherence", "completeness", "hallucination"],
        )

    assert db.execute.await_count == 1
    # And the one transcript actually reaches the judges, rather than each
    # of them quietly falling back to a read of its own.
    assert hallucination_mock.call_args.kwargs["transcript"] == [
        {"role": "user", "text": "hello"},
        {"role": "model", "text": "hi"},
    ]


@pytest.mark.asyncio
async def test_results_come_back_in_registry_order_not_completion_order():
    """Judges now finish in whatever order the network returns them. The
    rows must not: callers and screens read them in registry order."""
    db = _db()

    def _outcome_for(name):
        return AsyncMock(
            return_value=EvaluationOutcome(
                evaluator=name, kind="llm_judge", score=1.0, passed=True
            )
        )

    with (
        patch("apowerb.evaluation.run_service.evaluate_tool_execution_outcome",
              new=AsyncMock(return_value=EvaluationOutcome(
                  evaluator="tool_execution_outcome", kind="deterministic",
                  score=1.0, passed=True))),
        patch("apowerb.evaluation.run_service.evaluate_tool_usage",
              new=AsyncMock(return_value=EvaluationOutcome(
                  evaluator="tool_usage", kind="deterministic",
                  score=1.0, passed=True))),
        patch("apowerb.evaluation.run_service.evaluate_task_completion",
              new=_outcome_for("task_completion_judge")),
        patch("apowerb.evaluation.run_service.evaluate_coherence",
              new=_outcome_for("coherence")),
        patch("apowerb.evaluation.run_service.evaluate_completeness",
              new=_outcome_for("completeness")),
        patch("apowerb.evaluation.run_service.evaluate_hallucination",
              new=_outcome_for("hallucination")),
    ):
        # Asked for in a deliberately scrambled order.
        rows = await run_and_persist(
            db, _ctx(), "session_x",
            ["hallucination", "tool_usage", "task_completion_judge",
             "coherence", "tool_execution_outcome", "completeness"],
        )

    assert [row.evaluator_name for row in rows] == [
        "tool_execution_outcome",
        "task_completion_judge",
        "tool_usage",
        "coherence",
        "completeness",
        "hallucination",
    ]

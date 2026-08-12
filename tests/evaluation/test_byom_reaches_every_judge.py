"""What only the assembly could show: BYOM has to reach every judge.

The two branches were each correct on their own. One added a judge model
per request; the other added three judges reading the server model straight
from settings. Merged as-is, a caller's own model would have been honoured
by `task_completion_judge` and silently ignored by the other three — and
their tokens, real spend on a reasoning model, would have been billed to
nobody because they never declared `judge_usage`.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apowerb.evaluation.evaluators.base import EvaluationOutcome
from apowerb.evaluation.run_service import run_and_persist

JUDGES = ["task_completion_judge", "coherence", "completeness", "hallucination"]


def _ctx():
    ctx = MagicMock()
    ctx.agent_id = 1201
    ctx.agent_name = "DA_Dave"
    ctx.app_name = "agent1201"
    ctx.owner_id = "user@example.com"
    ctx.session_user_id = "user@example.com"
    ctx.judged_model = "gemini/gemini-3-flash-preview"
    return ctx


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
    db = AsyncMock()
    db.add = MagicMock()
    db.execute = AsyncMock(return_value=_transcript_rows())
    return db


def _outcome(name):
    return EvaluationOutcome(
        evaluator=name,
        kind="llm_judge",
        score=1.0,
        passed=True,
        details={"judge_model": "anthropic/claude", "judge_is_byom": True},
    )


@pytest.mark.parametrize("judge", JUDGES)
@pytest.mark.asyncio
async def test_a_caller_model_reaches_every_judge(judge):
    seen = {}

    async def capture(*args, **kwargs):
        seen.update(kwargs)
        return _outcome(judge)

    target = {
        "task_completion_judge": "apowerb.evaluation.run_service.evaluate_task_completion",
        "coherence": "apowerb.evaluation.run_service.evaluate_coherence",
        "completeness": "apowerb.evaluation.run_service.evaluate_completeness",
        "hallucination": "apowerb.evaluation.run_service.evaluate_hallucination",
    }[judge]

    with patch(target, new=AsyncMock(side_effect=capture)):
        await run_and_persist(
            _db(),
            _ctx(),
            "session_x",
            [judge],
            judge_model="anthropic/claude-sonnet-4",
            judge_api_key="sk-caller",
        )

    assert seen.get("judge_model") == "anthropic/claude-sonnet-4", judge
    assert seen.get("judge_api_key") == "sk-caller", judge


@pytest.mark.parametrize("judge", JUDGES)
@pytest.mark.asyncio
async def test_every_judge_declares_what_it_spent(judge):
    """`_record_judge_usage` reads `judge_usage`; a judge that omits it
    burns tokens nobody is billed for."""
    from apowerb.evaluation.evaluators import _shared_judge

    source = _shared_judge.__file__
    assert "extract_usage" in open(source, encoding="utf-8").read()

    module = {
        "coherence": "coherence",
        "completeness": "completeness",
        "hallucination": "hallucination",
        "task_completion_judge": "task_completion_judge",
    }[judge]
    path = source.replace("_shared_judge.py", f"{module}.py")
    text = open(path, encoding="utf-8").read()

    assert '"judge_usage"' in text, f"{judge} ne déclare pas ses tokens"
    assert '"judge_is_byom"' in text, f"{judge} ne dit pas qui paie"

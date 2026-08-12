"""Audit fixes for coherence.py / completeness.py / hallucination.py:
points 2, 3, 4, 6, 8 -- same shape as task_completion_judge, exercised
against the three judges built on `_shared_judge`.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apowerb.evaluation.evaluators.coherence import evaluate_coherence
from apowerb.evaluation.evaluators.completeness import evaluate_completeness
from apowerb.evaluation.evaluators.hallucination import evaluate_hallucination

_SHARED = "apowerb.evaluation.evaluators._shared_judge"

_CASES = [
    (evaluate_coherence, "coherence", "coherence"),
    (evaluate_completeness, "completeness", "completeness"),
    (evaluate_hallucination, "hallucination", "internal_plausibility"),
]


def _events_result(rows):
    result = MagicMock()
    result.fetchall.return_value = rows
    return result


def _transcript_db():
    db = AsyncMock()
    db.execute.return_value = _events_result(
        [({"content": {"role": "user", "parts": [{"text": "hello"}]}},)]
    )
    return db


def _settings():
    return MagicMock(
        evaluation_judge_model="gemini/gemini-2.5-pro",
        evaluation_judge_api_key="k",
        evaluation_pass_threshold_coherence=0.7,
        evaluation_pass_threshold_completeness=0.7,
        evaluation_pass_threshold_hallucination=0.7,
    )


def _reply(content, *, finish_reason="stop", usage=None):
    reply = MagicMock()
    choice = MagicMock()
    choice.message = MagicMock(content=content)
    choice.finish_reason = finish_reason
    reply.choices = [choice]
    reply.usage = usage or MagicMock(
        prompt_tokens=100,
        completion_tokens=20,
        total_tokens=120,
        completion_tokens_details=MagicMock(reasoning_tokens=5),
        prompt_tokens_details=MagicMock(cached_tokens=0),
    )
    return reply


@pytest.mark.parametrize("evaluate_fn,evaluator_name,score_key", _CASES)
class TestReasoningIsBounded:
    @pytest.mark.asyncio
    async def test_reasoning_effort_is_passed_to_litellm(
        self, evaluate_fn, evaluator_name, score_key
    ):
        captured = {}

        async def _capture(**kwargs):
            captured.update(kwargs)
            return _reply(f'{{"{score_key}": 1.0}}')

        with patch(f"{_SHARED}.get_settings", return_value=_settings()), patch(
            "litellm.acompletion", new=AsyncMock(side_effect=_capture)
        ):
            await evaluate_fn(
                _transcript_db(),
                app_name="agent1201",
                user_id="u",
                session_id="s",
                judged_model="openai/gpt-4o",
            )

        assert "reasoning_effort" in captured


@pytest.mark.parametrize("evaluate_fn,evaluator_name,score_key", _CASES)
class TestTruncatedJsonIsDistinguished:
    @pytest.mark.asyncio
    async def test_truncated_json_is_reported_as_truncated(
        self, evaluate_fn, evaluator_name, score_key
    ):
        with patch(f"{_SHARED}.get_settings", return_value=_settings()), patch(
            "litellm.acompletion",
            new=AsyncMock(
                return_value=_reply(f'{{"{score_key}": 0.', finish_reason="length")
            ),
        ):
            with pytest.raises(ValueError) as excinfo:
                await evaluate_fn(
                    _transcript_db(),
                    app_name="agent1201",
                    user_id="u",
                    session_id="s",
                    judged_model="openai/gpt-4o",
                )

        assert "truncated" in str(excinfo.value).lower()
        assert excinfo.value.judge_usage["total_tokens"] == 120
        assert excinfo.value.judge_model == "gemini/gemini-2.5-pro"


@pytest.mark.parametrize("evaluate_fn,evaluator_name,score_key", _CASES)
class TestMissingKeyIsNotAZero:
    @pytest.mark.asyncio
    async def test_missing_score_key_raises_and_carries_billing(
        self, evaluate_fn, evaluator_name, score_key
    ):
        with patch(f"{_SHARED}.get_settings", return_value=_settings()), patch(
            "litellm.acompletion",
            new=AsyncMock(return_value=_reply('{"rationale": "ok"}')),
        ):
            with pytest.raises(ValueError) as excinfo:
                await evaluate_fn(
                    _transcript_db(),
                    app_name="agent1201",
                    user_id="u",
                    session_id="s",
                    judged_model="openai/gpt-4o",
                )

        assert score_key in str(excinfo.value)
        assert excinfo.value.judge_usage is not None
        assert excinfo.value.judge_model == "gemini/gemini-2.5-pro"


@pytest.mark.parametrize("evaluate_fn,evaluator_name,score_key", _CASES)
class TestTranscriptTailTruncation:
    @pytest.mark.asyncio
    async def test_overflowing_transcript_sends_the_tail_and_flags_it(
        self, evaluate_fn, evaluator_name, score_key
    ):
        long_events = [
            ({"content": {"role": "user", "parts": [{"text": f"turn {i} " * 50}]}},)
            for i in range(200)
        ]
        db = AsyncMock()
        db.execute.return_value = _events_result(long_events)
        captured = {}

        async def _capture(**kwargs):
            captured.update(kwargs)
            return _reply(f'{{"{score_key}": 1.0}}')

        with patch(f"{_SHARED}.get_settings", return_value=_settings()), patch(
            "litellm.acompletion", new=AsyncMock(side_effect=_capture)
        ):
            outcome = await evaluate_fn(
                db,
                app_name="agent1201",
                user_id="u",
                session_id="s",
                judged_model="openai/gpt-4o",
            )

        sent_transcript = captured["messages"][1]["content"]
        assert "turn 199" in sent_transcript
        assert "turn 0 " not in sent_transcript
        assert outcome.details["truncated"] is True

    @pytest.mark.asyncio
    async def test_short_transcript_is_not_flagged_truncated(
        self, evaluate_fn, evaluator_name, score_key
    ):
        with patch(f"{_SHARED}.get_settings", return_value=_settings()), patch(
            "litellm.acompletion",
            new=AsyncMock(return_value=_reply(f'{{"{score_key}": 1.0}}')),
        ):
            outcome = await evaluate_fn(
                _transcript_db(),
                app_name="agent1201",
                user_id="u",
                session_id="s",
                judged_model="openai/gpt-4o",
            )

        assert outcome.details["truncated"] is False


@pytest.mark.parametrize("evaluate_fn,evaluator_name,score_key", _CASES)
class TestPassedFollowsThreshold:
    @pytest.mark.asyncio
    async def test_below_threshold_does_not_pass(
        self, evaluate_fn, evaluator_name, score_key
    ):
        with patch(f"{_SHARED}.get_settings", return_value=_settings()), patch(
            "litellm.acompletion",
            new=AsyncMock(return_value=_reply(f'{{"{score_key}": 0.5}}')),
        ):
            outcome = await evaluate_fn(
                _transcript_db(),
                app_name="agent1201",
                user_id="u",
                session_id="s",
                judged_model="openai/gpt-4o",
            )

        assert outcome.score == 0.5
        assert outcome.passed is False

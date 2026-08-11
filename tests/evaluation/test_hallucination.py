"""Unit tests for the LLM-judge hallucination evaluator.

Degraded version, assumed: the RAG tool does not log its retrieved chunks
separately from its response, so there is no ground truth to check the
agent's claims against. This evaluator judges internal plausibility of the
transcript only -- never a real groundedness score -- and must say so in
`details["grounding"]`.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apowerb.evaluation.evaluators.hallucination import evaluate_hallucination
from apowerb.evaluation.evaluators.task_completion_judge import SameJudgeError

_MODULE = "apowerb.evaluation.evaluators.hallucination"


def _events_result(rows):
    result = MagicMock()
    result.fetchall.return_value = rows
    return result


@pytest.mark.asyncio
async def test_refuses_to_judge_a_model_with_itself():
    db = AsyncMock()
    with patch(f"{_MODULE}.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(
            evaluation_judge_model="gemini/gemini-2.5-pro",
            evaluation_judge_api_key="k",
        )
        with pytest.raises(SameJudgeError):
            await evaluate_hallucination(
                db,
                app_name="agent1234",
                user_id="u",
                session_id="s",
                judged_model="gemini/gemini-2.5-pro",
            )
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_missing_judge_config_raises_before_any_query():
    db = AsyncMock()
    with patch(f"{_MODULE}.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(
            evaluation_judge_model="", evaluation_judge_api_key=""
        )
        with pytest.raises(RuntimeError):
            await evaluate_hallucination(
                db,
                app_name="agent1234",
                user_id="u",
                session_id="s",
                judged_model="gemini/gemini-2.5-flash",
            )
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_empty_transcript_is_not_applicable_not_a_zero():
    db = AsyncMock()
    db.execute.return_value = _events_result([])

    with patch(f"{_MODULE}.get_settings") as mock_settings, patch(
        "litellm.acompletion", new_callable=AsyncMock
    ) as mock_completion:
        mock_settings.return_value = MagicMock(
            evaluation_judge_model="gemini/gemini-2.5-pro",
            evaluation_judge_api_key="k",
        )
        outcome = await evaluate_hallucination(
            db,
            app_name="agent1201",
            user_id="u",
            session_id="empty",
            judged_model="openai/Mistral-Small-3.2-24B-Instruct-2506",
        )

    assert outcome.score is None
    assert outcome.applicable is False
    mock_completion.assert_not_awaited()


@pytest.mark.asyncio
async def test_scores_a_real_shaped_transcript_and_flags_grounding_unavailable():
    db = AsyncMock()
    db.execute.return_value = _events_result(
        [
            (
                {
                    "author": "user",
                    "content": {"role": "user", "parts": [{"text": "how many rows in ventes?"}]},
                },
            ),
            (
                {
                    "author": "agent1201",
                    "content": {
                        "role": "model",
                        "parts": [{"text": "There are exactly 4,213,908 rows, retrieved on Mars."}],
                    },
                },
            ),
        ]
    )

    fake_response = MagicMock()
    fake_response.choices = [
        MagicMock(
            message=MagicMock(
                content=(
                    '{"internal_plausibility": 0.1, "rationale": '
                    '"The claim (4,213,908 rows, retrieved on Mars) is internally '
                    'implausible and unsupported by anything earlier in the transcript."}'
                )
            )
        )
    ]

    with patch(f"{_MODULE}.get_settings") as mock_settings, patch(
        "litellm.acompletion", new_callable=AsyncMock
    ) as mock_completion:
        mock_settings.return_value = MagicMock(
            evaluation_judge_model="gemini/gemini-2.5-pro",
            evaluation_judge_api_key="k",
        )
        mock_completion.return_value = fake_response

        outcome = await evaluate_hallucination(
            db,
            app_name="agent1201",
            user_id="user@example.com",
            session_id="dashboard-chat-b59ebab5",
            judged_model="openai/Mistral-Small-3.2-24B-Instruct-2506",
        )

    assert outcome.score == pytest.approx(0.1)
    assert outcome.passed is False
    assert outcome.details["grounding"] == "unavailable"
    assert outcome.details["internal_plausibility"] == 0.1
    assert "implausible" in outcome.details["rationale"]
    call_kwargs = mock_completion.call_args.kwargs
    assert call_kwargs["max_tokens"] == 2000


@pytest.mark.asyncio
async def test_score_never_looks_like_a_grounding_score_in_details_keys():
    """The score key must not be named/aliased so it could be mistaken for
    an anchored-in-sources groundedness score anywhere it's read."""
    db = AsyncMock()
    db.execute.return_value = _events_result(
        [({"content": {"role": "user", "parts": [{"text": "hi"}]}},)]
    )

    fake_response = MagicMock()
    fake_response.choices = [
        MagicMock(
            message=MagicMock(
                content='{"internal_plausibility": 1.0, "rationale": "fine"}'
            )
        )
    ]

    with patch(f"{_MODULE}.get_settings") as mock_settings, patch(
        "litellm.acompletion", new_callable=AsyncMock, return_value=fake_response
    ):
        mock_settings.return_value = MagicMock(
            evaluation_judge_model="gemini/gemini-2.5-pro",
            evaluation_judge_api_key="k",
        )
        outcome = await evaluate_hallucination(
            db,
            app_name="agent1201",
            user_id="u",
            session_id="s",
            judged_model="openai/Mistral-Small-3.2-24B-Instruct-2506",
        )

    assert "groundedness" not in outcome.details
    assert "grounding_score" not in outcome.details
    assert outcome.details["grounding"] == "unavailable"

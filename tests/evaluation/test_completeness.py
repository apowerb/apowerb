"""Unit tests for the LLM-judge completeness evaluator."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apowerb.evaluation.evaluators.completeness import evaluate_completeness
from apowerb.evaluation.evaluators.task_completion_judge import SameJudgeError

_MODULE = "apowerb.evaluation.evaluators.completeness"
# The judge model is resolved in _shared_judge now, not here: patching
# get_settings on this module would no longer intercept the read.
_SHARED = "apowerb.evaluation.evaluators._shared_judge"


def _events_result(rows):
    result = MagicMock()
    result.fetchall.return_value = rows
    return result


@pytest.mark.asyncio
async def test_refuses_to_judge_a_model_with_itself():
    db = AsyncMock()
    with patch(f"{_SHARED}.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(
            evaluation_judge_model="gemini/gemini-2.5-pro",
            evaluation_judge_api_key="k",
        )
        with pytest.raises(SameJudgeError):
            await evaluate_completeness(
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
    with patch(f"{_SHARED}.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(
            evaluation_judge_model="", evaluation_judge_api_key=""
        )
        with pytest.raises(RuntimeError):
            await evaluate_completeness(
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

    with patch(f"{_SHARED}.get_settings") as mock_settings, patch(
        "litellm.acompletion", new_callable=AsyncMock
    ) as mock_completion:
        mock_settings.return_value = MagicMock(
            evaluation_judge_model="gemini/gemini-2.5-pro",
            evaluation_judge_api_key="k",
        )
        outcome = await evaluate_completeness(
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
async def test_scores_a_real_shaped_transcript():
    db = AsyncMock()
    db.execute.return_value = _events_result(
        [
            (
                {
                    "author": "user",
                    "content": {
                        "role": "user",
                        "parts": [{"text": "show sales AND the schema"}],
                    },
                },
            ),
            (
                {
                    "author": "agent1201",
                    "content": {"role": "model", "parts": [{"text": "Here is the schema."}]},
                },
            ),
        ]
    )

    fake_response = MagicMock()
    fake_response.choices = [
        MagicMock(
            message=MagicMock(
                content=(
                    '{"completeness": 0.5, "rationale": '
                    '"Only the schema was returned, sales data is missing."}'
                )
            )
        )
    ]

    with patch(f"{_SHARED}.get_settings") as mock_settings, patch(
        "litellm.acompletion", new_callable=AsyncMock
    ) as mock_completion:
        mock_settings.return_value = MagicMock(
            evaluation_judge_model="gemini/gemini-2.5-pro",
            evaluation_judge_api_key="k",
        )
        mock_completion.return_value = fake_response

        outcome = await evaluate_completeness(
            db,
            app_name="agent1201",
            user_id="user@example.com",
            session_id="dashboard-chat-b59ebab5",
            judged_model="openai/Mistral-Small-3.2-24B-Instruct-2506",
        )

    assert outcome.score == pytest.approx(0.5)
    assert outcome.passed is False
    assert outcome.details["turns"] == 2
    assert outcome.details["judge_model"] == "gemini/gemini-2.5-pro"
    assert "missing" in outcome.details["rationale"]
    mock_completion.assert_awaited_once()
    call_kwargs = mock_completion.call_args.kwargs
    assert call_kwargs["max_tokens"] == 2000

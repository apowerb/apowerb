"""Unit tests for the LLM-judge task-completion evaluator."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apowerb.evaluation.evaluators.task_completion_judge import (
    SameJudgeError,
    evaluate_task_completion,
)

_MODULE = "apowerb.evaluation.evaluators.task_completion_judge"


def _events_result(rows):
    result = MagicMock()
    result.fetchall.return_value = rows
    return result


@pytest.mark.asyncio
async def test_refuses_to_judge_a_model_with_itself():
    db = AsyncMock()
    with patch(f"{_MODULE}.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(
            evaluation_judge_model="gemini/gemini-2.5-flash",
            evaluation_judge_api_key="k",
        )
        with pytest.raises(SameJudgeError):
            await evaluate_task_completion(
                db,
                app_name="agent1234",
                user_id="u",
                session_id="s",
                judged_model="gemini/gemini-2.5-flash",
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
            await evaluate_task_completion(
                db,
                app_name="agent1234",
                user_id="u",
                session_id="s",
                judged_model="gemini/gemini-2.5-flash",
            )
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_scores_a_real_shaped_transcript():
    """Event shape mirrors a real DEV session (dashboard-chat-b59ebab5...,
    agent1201, model Mistral-Small-3.2-24B-Instruct-2506) -- a text turn,
    a function_call turn, a text turn.
    """
    db = AsyncMock()
    db.execute.return_value = _events_result(
        [
            (
                {
                    "author": "user",
                    "content": {
                        "role": "user",
                        "parts": [{"text": "crée ce graphique en camembert"}],
                    },
                },
            ),
            (
                {
                    "author": "agent1201",
                    "content": {
                        "role": "model",
                        "parts": [
                            {
                                "function_call": {
                                    "name": "tool_get_dashboard_data",
                                    "args": {"dashboard_id": "b59ebab5"},
                                }
                            }
                        ],
                    },
                },
            ),
            (
                {
                    "author": "agent1201",
                    "content": {
                        "role": "model",
                        "parts": [{"text": "Voici le graphique en camembert."}],
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
                    '{"task_completion": 0.9, "intent_resolution": 1.0, '
                    '"rationale": "Pie chart delivered as asked."}'
                )
            )
        )
    ]

    with patch(f"{_MODULE}.get_settings") as mock_settings, patch(
        "litellm.acompletion", new_callable=AsyncMock
    ) as mock_completion:
        mock_settings.return_value = MagicMock(
            evaluation_judge_model="gemini/gemini-2.5-flash",
            evaluation_judge_api_key="k",
        )
        mock_completion.return_value = fake_response

        outcome = await evaluate_task_completion(
            db,
            app_name="agent1201",
            user_id="elom.gnaglo@gmail.com",
            session_id="dashboard-chat-b59ebab5-9e8c-44b1-b113-b56ae179c6ce",
            judged_model="openai/Mistral-Small-3.2-24B-Instruct-2506",
        )

    assert outcome.score == pytest.approx(0.95)
    assert outcome.passed is True
    assert outcome.details["turns"] == 3
    assert outcome.details["judge_model"] == "gemini/gemini-2.5-flash"
    mock_completion.assert_awaited_once()
    call_kwargs = mock_completion.call_args.kwargs
    assert call_kwargs["model"] == "gemini/gemini-2.5-flash"


@pytest.mark.asyncio
async def test_empty_transcript_short_circuits_without_calling_the_judge():
    db = AsyncMock()
    db.execute.return_value = _events_result([])

    with patch(f"{_MODULE}.get_settings") as mock_settings, patch(
        "litellm.acompletion", new_callable=AsyncMock
    ) as mock_completion:
        mock_settings.return_value = MagicMock(
            evaluation_judge_model="gemini/gemini-2.5-flash",
            evaluation_judge_api_key="k",
        )

        outcome = await evaluate_task_completion(
            db,
            app_name="agent1201",
            user_id="u",
            session_id="empty",
            judged_model="openai/Mistral-Small-3.2-24B-Instruct-2506",
        )

    assert outcome.score == 0.0
    mock_completion.assert_not_awaited()

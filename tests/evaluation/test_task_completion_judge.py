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
            user_id="user@example.com",
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
async def test_empty_transcript_is_not_applicable_not_a_zero():
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

    assert outcome.score is None
    mock_completion.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_judge_of_the_same_provider_is_recorded_in_the_details():
    """Not refused — an install with one provider must still evaluate — but
    self-preference is documented at the provider level, so the score must
    carry the caveat that may have tilted it."""
    db = AsyncMock()
    db.execute.return_value = _events_result(
        [({"content": {"role": "user", "parts": [{"text": "hello"}]}},)]
    )

    judge_reply = MagicMock()
    judge_reply.choices = [
        MagicMock(
            message=MagicMock(
                content='{"task_completion": 1.0, "intent_resolution": 1.0, "rationale": "fine"}'
            )
        )
    ]

    with patch(f"{_MODULE}.get_settings") as mock_settings, patch(
        "litellm.acompletion", new_callable=AsyncMock, return_value=judge_reply
    ):
        mock_settings.return_value = MagicMock(
            evaluation_judge_model="gemini/gemini-2.5-flash",
            evaluation_judge_api_key="k",
        )

        outcome = await evaluate_task_completion(
            db,
            app_name="agent1201",
            user_id="u",
            session_id="s",
            judged_model="gemini/gemini-2.5-pro",
        )

    assert outcome.details["judge_shares_provider_with_judged"] is True


# ---------------------------------------------------------------------------
# Bring-your-own-model (BYOM) judge
# ---------------------------------------------------------------------------


def _single_turn_events():
    return _events_result(
        [({"content": {"role": "user", "parts": [{"text": "hello"}]}},)]
    )


def _judge_reply(task_completion=1.0, intent_resolution=1.0, rationale="fine", usage=None):
    reply = MagicMock()
    reply.choices = [
        MagicMock(
            message=MagicMock(
                content=(
                    f'{{"task_completion": {task_completion}, '
                    f'"intent_resolution": {intent_resolution}, '
                    f'"rationale": "{rationale}"}}'
                )
            )
        )
    ]
    reply.usage = usage
    return reply


@pytest.mark.asyncio
async def test_byom_judge_model_and_key_are_used_over_server_settings():
    db = AsyncMock()
    db.execute.return_value = _single_turn_events()

    with patch(f"{_MODULE}.get_settings") as mock_settings, patch(
        "litellm.acompletion", new_callable=AsyncMock
    ) as mock_completion:
        mock_settings.return_value = MagicMock(
            evaluation_judge_model="gemini/gemini-2.5-flash",
            evaluation_judge_api_key="server-key",
        )
        mock_completion.return_value = _judge_reply()

        outcome = await evaluate_task_completion(
            db,
            app_name="agent1201",
            user_id="u",
            session_id="s",
            judged_model="openai/Mistral-Small-3.2-24B-Instruct-2506",
            judge_model="anthropic/claude-3-5-sonnet",
            judge_api_key="byom-secret-key",
        )

    call_kwargs = mock_completion.call_args.kwargs
    assert call_kwargs["model"] == "anthropic/claude-3-5-sonnet"
    assert call_kwargs["api_key"] == "byom-secret-key"
    assert outcome.details["judge_model"] == "anthropic/claude-3-5-sonnet"
    assert outcome.details["judge_is_byom"] is True


@pytest.mark.asyncio
async def test_server_default_judge_marks_details_not_byom():
    db = AsyncMock()
    db.execute.return_value = _single_turn_events()

    with patch(f"{_MODULE}.get_settings") as mock_settings, patch(
        "litellm.acompletion", new_callable=AsyncMock
    ) as mock_completion:
        mock_settings.return_value = MagicMock(
            evaluation_judge_model="gemini/gemini-2.5-flash",
            evaluation_judge_api_key="server-key",
        )
        mock_completion.return_value = _judge_reply()

        outcome = await evaluate_task_completion(
            db,
            app_name="agent1201",
            user_id="u",
            session_id="s",
            judged_model="openai/Mistral-Small-3.2-24B-Instruct-2506",
        )

    call_kwargs = mock_completion.call_args.kwargs
    assert call_kwargs["model"] == "gemini/gemini-2.5-flash"
    assert call_kwargs["api_key"] == "server-key"
    assert outcome.details["judge_is_byom"] is False


@pytest.mark.asyncio
async def test_byom_judge_equal_to_judged_model_still_refused():
    """The self-preference guard checks the model EFFECTIVELY retained
    (the caller's BYOM choice), not the server's configured judge -- a
    caller could otherwise dodge the guard by supplying their own model
    even though it matches what they are asking to be judged."""
    db = AsyncMock()

    with patch(f"{_MODULE}.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(
            evaluation_judge_model="gemini/gemini-2.5-flash",
            evaluation_judge_api_key="server-key",
        )
        with pytest.raises(SameJudgeError):
            await evaluate_task_completion(
                db,
                app_name="agent1201",
                user_id="u",
                session_id="s",
                judged_model="openai/gpt-4o",
                judge_model="openai/gpt-4o",
                judge_api_key="byom-secret-key",
            )
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_byom_judge_model_without_key_raises_before_any_query():
    """Defense in depth: the router is the primary 400 gate, but this
    evaluator must never silently fall back to the server's key just
    because the caller forgot theirs."""
    db = AsyncMock()

    with patch(f"{_MODULE}.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(
            evaluation_judge_model="gemini/gemini-2.5-flash",
            evaluation_judge_api_key="server-key",
        )
        with pytest.raises(RuntimeError):
            await evaluate_task_completion(
                db,
                app_name="agent1201",
                user_id="u",
                session_id="s",
                judged_model="openai/gpt-4o",
                judge_model="anthropic/claude-3-5-sonnet",
                judge_api_key="",
            )
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_byom_api_key_never_leaks_into_details_or_logs(caplog):
    db = AsyncMock()
    db.execute.return_value = _single_turn_events()
    secret = "sk-super-secret-byom-token-should-never-appear"

    with patch(f"{_MODULE}.get_settings") as mock_settings, patch(
        "litellm.acompletion", new_callable=AsyncMock
    ) as mock_completion:
        mock_settings.return_value = MagicMock(
            evaluation_judge_model="gemini/gemini-2.5-flash",
            evaluation_judge_api_key="server-key",
        )
        mock_completion.return_value = _judge_reply()

        with caplog.at_level("DEBUG"):
            outcome = await evaluate_task_completion(
                db,
                app_name="agent1201",
                user_id="u",
                session_id="s",
                judged_model="openai/Mistral-Small-3.2-24B-Instruct-2506",
                judge_model="anthropic/claude-3-5-sonnet",
                judge_api_key=secret,
            )

    assert secret not in str(outcome.details)
    assert secret not in caplog.text


# ---------------------------------------------------------------------------
# Judge token usage extraction (for llm_usage accounting)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_judge_usage_tokens_are_extracted_from_the_litellm_response():
    db = AsyncMock()
    db.execute.return_value = _single_turn_events()

    usage = MagicMock()
    usage.prompt_tokens = 5200
    usage.completion_tokens = 120
    usage.total_tokens = 5320
    usage.completion_tokens_details = MagicMock(reasoning_tokens=95)
    usage.prompt_tokens_details = MagicMock(cached_tokens=1000)

    with patch(f"{_MODULE}.get_settings") as mock_settings, patch(
        "litellm.acompletion", new_callable=AsyncMock
    ) as mock_completion:
        mock_settings.return_value = MagicMock(
            evaluation_judge_model="gemini/gemini-2.5-flash",
            evaluation_judge_api_key="server-key",
        )
        mock_completion.return_value = _judge_reply(usage=usage)

        outcome = await evaluate_task_completion(
            db,
            app_name="agent1201",
            user_id="u",
            session_id="s",
            judged_model="openai/Mistral-Small-3.2-24B-Instruct-2506",
        )

    assert outcome.details["judge_usage"] == {
        "input_tokens": 5200,
        "output_tokens": 120,
        "thoughts_tokens": 95,
        "cached_tokens": 1000,
        "total_tokens": 5320,
    }


@pytest.mark.asyncio
async def test_judge_usage_defaults_to_zeros_when_response_has_no_usage():
    db = AsyncMock()
    db.execute.return_value = _single_turn_events()

    with patch(f"{_MODULE}.get_settings") as mock_settings, patch(
        "litellm.acompletion", new_callable=AsyncMock
    ) as mock_completion:
        mock_settings.return_value = MagicMock(
            evaluation_judge_model="gemini/gemini-2.5-flash",
            evaluation_judge_api_key="server-key",
        )
        reply = _judge_reply(usage=None)
        mock_completion.return_value = reply

        outcome = await evaluate_task_completion(
            db,
            app_name="agent1201",
            user_id="u",
            session_id="s",
            judged_model="openai/Mistral-Small-3.2-24B-Instruct-2506",
        )

    assert outcome.details["judge_usage"] == {
        "input_tokens": 0,
        "output_tokens": 0,
        "thoughts_tokens": 0,
        "cached_tokens": 0,
        "total_tokens": 0,
    }


# ---------------------------------------------------------------------------
# criteria: ordered list of what the evaluator measured
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_details_carries_ordered_criteria():
    db = AsyncMock()
    db.execute.return_value = _events_result(
        [({"content": {"role": "user", "parts": [{"text": "hello"}]}},)]
    )
    reply = MagicMock()
    reply.choices = [
        MagicMock(
            message=MagicMock(
                content='{"task_completion": 0.8, "intent_resolution": 0.6, "rationale": "ok"}'
            )
        )
    ]

    with patch(f"{_MODULE}.get_settings") as mock_settings, patch(
        "litellm.acompletion", new_callable=AsyncMock, return_value=reply
    ):
        mock_settings.return_value = MagicMock(
            evaluation_judge_model="gemini/gemini-2.5-flash",
            evaluation_judge_api_key="k",
        )
        outcome = await evaluate_task_completion(
            db,
            app_name="agent1201",
            user_id="u",
            session_id="s",
            judged_model="openai/gpt-4o",
        )

    assert outcome.details["criteria"] == [
        {"name": "task_completion", "value": 0.8, "kind": "score"},
        {"name": "intent_resolution", "value": 0.6, "kind": "score"},
        {"name": "turns", "value": 1, "kind": "count"},
    ]
    # rationale is a sentence, not a measurement -- it must stay out of criteria.
    assert "rationale" not in [c["name"] for c in outcome.details["criteria"]]


# ---------------------------------------------------------------------------
# locale: rationale language follows the interface, not the judged session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_locale_fr_asks_the_judge_for_a_french_rationale():
    db = AsyncMock()
    db.execute.return_value = _events_result(
        [({"content": {"role": "user", "parts": [{"text": "bonjour"}]}},)]
    )
    reply = MagicMock()
    reply.choices = [
        MagicMock(
            message=MagicMock(
                content='{"task_completion": 1.0, "intent_resolution": 1.0, "rationale": "Fait."}'
            )
        )
    ]

    with patch(f"{_MODULE}.get_settings") as mock_settings, patch(
        "litellm.acompletion", new_callable=AsyncMock, return_value=reply
    ) as mock_completion:
        mock_settings.return_value = MagicMock(
            evaluation_judge_model="gemini/gemini-2.5-flash",
            evaluation_judge_api_key="k",
        )
        await evaluate_task_completion(
            db, app_name="agent1201", user_id="u", session_id="s",
            judged_model="openai/gpt-4o", locale="fr",
        )

    system_prompt = mock_completion.call_args.kwargs["messages"][0]["content"]
    assert "in French" in system_prompt
    assert "in English" not in system_prompt


@pytest.mark.asyncio
async def test_missing_locale_defaults_to_english():
    db = AsyncMock()
    db.execute.return_value = _events_result(
        [({"content": {"role": "user", "parts": [{"text": "hi"}]}},)]
    )
    reply = MagicMock()
    reply.choices = [
        MagicMock(
            message=MagicMock(
                content='{"task_completion": 1.0, "intent_resolution": 1.0, "rationale": "Done."}'
            )
        )
    ]

    with patch(f"{_MODULE}.get_settings") as mock_settings, patch(
        "litellm.acompletion", new_callable=AsyncMock, return_value=reply
    ) as mock_completion:
        mock_settings.return_value = MagicMock(
            evaluation_judge_model="gemini/gemini-2.5-flash",
            evaluation_judge_api_key="k",
        )
        await evaluate_task_completion(
            db, app_name="agent1201", user_id="u", session_id="s",
            judged_model="openai/gpt-4o",
        )

    system_prompt = mock_completion.call_args.kwargs["messages"][0]["content"]
    assert "in English" in system_prompt

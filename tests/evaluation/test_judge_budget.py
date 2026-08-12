"""The judge has to survive a reasoning model.

Measured on the dev machine, 2026-08-10: gemini-2.5-pro spends its completion
budget on reasoning before it writes a word — ~110 reasoning tokens for 15 of
answer on a trivial prompt. On a real 20k-character transcript the previous
budget of 400 was consumed entirely by the reasoning, litellm returned
`content=None`, and the failure surfaced as "judge did not return a JSON
object: None" — which reads like a prompt problem and is not one.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apowerb.evaluation.evaluators.task_completion_judge import (
    evaluate_task_completion,
)

_MODULE = "apowerb.evaluation.evaluators.task_completion_judge"


def _events_result(rows):
    result = MagicMock()
    result.fetchall.return_value = rows
    return result


def _transcript_db():
    db = AsyncMock()
    db.execute.return_value = _events_result(
        [({"content": {"role": "user", "parts": [{"text": "bonjour"}]}},)]
    )
    return db


def _settings():
    return MagicMock(
        evaluation_judge_model="gemini/gemini-2.5-pro",
        evaluation_judge_api_key="k", evaluation_pass_threshold_task_completion=0.7,
    )


def _reply(content):
    reply = MagicMock()
    choice = MagicMock()
    choice.message = MagicMock(content=content)
    choice.finish_reason = "length"
    reply.choices = [choice]
    reply.usage = MagicMock(
        completion_tokens_details=MagicMock(reasoning_tokens=400)
    )
    return reply


class TestBudget:
    @pytest.mark.asyncio
    async def test_the_budget_leaves_room_for_an_answer(self):
        """400 was the reasoning of a short prompt, not an answer."""
        captured = {}

        async def _capture(**kwargs):
            captured.update(kwargs)
            return _reply('{"task_completion": 1.0, "intent_resolution": 1.0}')

        with patch(f"{_MODULE}.get_settings", return_value=_settings()), patch(
            "litellm.acompletion", new=AsyncMock(side_effect=_capture)
        ):
            await evaluate_task_completion(
                _transcript_db(),
                app_name="agent1201",
                user_id="user@example.com",
                session_id="s",
                judged_model="mistral/Mistral-Small-3.2-24B-Instruct-2506",
            )

        assert captured["max_tokens"] >= 2000

    @pytest.mark.asyncio
    async def test_an_empty_answer_says_the_budget_ran_out(self):
        """Not "no JSON": that sends the reader to the prompt instead."""
        with patch(f"{_MODULE}.get_settings", return_value=_settings()), patch(
            "litellm.acompletion", new=AsyncMock(return_value=_reply(None))
        ):
            with pytest.raises(RuntimeError) as excinfo:
                await evaluate_task_completion(
                    _transcript_db(),
                    app_name="agent1201",
                    user_id="user@example.com",
                    session_id="s",
                    judged_model="mistral/Mistral-Small-3.2-24B-Instruct-2506",
                )

        message = str(excinfo.value)
        assert "no content" in message
        assert "reasoning_tokens=400" in message

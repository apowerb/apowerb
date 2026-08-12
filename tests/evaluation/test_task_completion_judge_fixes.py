"""Audit fixes for task_completion_judge.py: points 2, 3, 4, 6, 8.

- point 2: reasoning is bounded (`reasoning_effort`), and a truncated JSON
  reply is reported as a truncation, not a generic parse failure.
- point 3: a missing required key is a non-applicable outcome, never a 0.0.
- point 4: any post-call failure still carries what the call cost, so it
  can be billed.
- point 6: the transcript sent to the judge keeps the END when it
  overflows, and `details["truncated"]` says so.
- point 8: `passed` follows the same composite `score` the screen shows,
  not `task_completion` alone.
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
        [({"content": {"role": "user", "parts": [{"text": "hello"}]}},)]
    )
    return db


def _settings():
    return MagicMock(
        evaluation_judge_model="gemini/gemini-2.5-pro",
        evaluation_judge_api_key="k",
        evaluation_pass_threshold_task_completion=0.7,
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


class TestReasoningIsBounded:
    @pytest.mark.asyncio
    async def test_reasoning_effort_is_passed_to_litellm(self):
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
                user_id="u",
                session_id="s",
                judged_model="openai/gpt-4o",
            )

        assert "reasoning_effort" in captured


class TestTruncatedJsonIsDistinguished:
    @pytest.mark.asyncio
    async def test_truncated_json_reason_says_truncated_not_generic_parse_failure(self):
        with patch(f"{_MODULE}.get_settings", return_value=_settings()), patch(
            "litellm.acompletion",
            new=AsyncMock(
                return_value=_reply(
                    '```json\n{\n  "task_completion": 1.0,\n  "intent_resolution": 1',
                    finish_reason="length",
                )
            ),
        ):
            with pytest.raises(ValueError) as excinfo:
                await evaluate_task_completion(
                    _transcript_db(),
                    app_name="agent1201",
                    user_id="u",
                    session_id="s",
                    judged_model="openai/gpt-4o",
                )

        assert "truncated" in str(excinfo.value).lower()
        assert "finish_reason=length" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_truncated_json_still_carries_its_billing(self):
        with patch(f"{_MODULE}.get_settings", return_value=_settings()), patch(
            "litellm.acompletion",
            new=AsyncMock(
                return_value=_reply(
                    '{"task_completion": 1.0, "intent', finish_reason="length"
                )
            ),
        ):
            with pytest.raises(ValueError) as excinfo:
                await evaluate_task_completion(
                    _transcript_db(),
                    app_name="agent1201",
                    user_id="u",
                    session_id="s",
                    judged_model="openai/gpt-4o",
                )

        assert excinfo.value.judge_usage["total_tokens"] == 120
        assert excinfo.value.judge_model == "gemini/gemini-2.5-pro"


class TestMissingKeyIsNotAZero:
    @pytest.mark.asyncio
    async def test_missing_intent_resolution_raises_not_scores_zero(self):
        with patch(f"{_MODULE}.get_settings", return_value=_settings()), patch(
            "litellm.acompletion",
            new=AsyncMock(
                return_value=_reply('{"task_completion": 0.9, "rationale": "ok"}')
            ),
        ):
            with pytest.raises(ValueError) as excinfo:
                await evaluate_task_completion(
                    _transcript_db(),
                    app_name="agent1201",
                    user_id="u",
                    session_id="s",
                    judged_model="openai/gpt-4o",
                )

        assert "intent_resolution" in str(excinfo.value)
        assert excinfo.value.judge_usage is not None
        assert excinfo.value.judge_model == "gemini/gemini-2.5-pro"


class TestTranscriptTailTruncation:
    @pytest.mark.asyncio
    async def test_overflowing_transcript_sends_the_tail_and_flags_it(self):
        long_events = [
            ({"content": {"role": "user", "parts": [{"text": f"turn {i} " * 50}]}},)
            for i in range(200)
        ]
        db = AsyncMock()
        db.execute.return_value = _events_result(long_events)
        captured = {}

        async def _capture(**kwargs):
            captured.update(kwargs)
            return _reply('{"task_completion": 1.0, "intent_resolution": 1.0}')

        with patch(f"{_MODULE}.get_settings", return_value=_settings()), patch(
            "litellm.acompletion", new=AsyncMock(side_effect=_capture)
        ):
            outcome = await evaluate_task_completion(
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
    async def test_short_transcript_is_not_flagged_truncated(self):
        with patch(f"{_MODULE}.get_settings", return_value=_settings()), patch(
            "litellm.acompletion",
            new=AsyncMock(
                return_value=_reply('{"task_completion": 1.0, "intent_resolution": 1.0}')
            ),
        ):
            outcome = await evaluate_task_completion(
                _transcript_db(),
                app_name="agent1201",
                user_id="u",
                session_id="s",
                judged_model="openai/gpt-4o",
            )

        assert outcome.details["truncated"] is False


class TestPassedFollowsTheScore:
    @pytest.mark.asyncio
    async def test_high_task_completion_low_intent_resolution_does_not_pass(self):
        """task_completion=1.0 alone used to set passed=True even though the
        composite score the screen shows is 0.5 -- badge and score must
        never disagree."""
        with patch(f"{_MODULE}.get_settings", return_value=_settings()), patch(
            "litellm.acompletion",
            new=AsyncMock(
                return_value=_reply(
                    '{"task_completion": 1.0, "intent_resolution": 0.0}'
                )
            ),
        ):
            outcome = await evaluate_task_completion(
                _transcript_db(),
                app_name="agent1201",
                user_id="u",
                session_id="s",
                judged_model="openai/gpt-4o",
            )

        assert outcome.score == 0.5
        assert outcome.passed is False

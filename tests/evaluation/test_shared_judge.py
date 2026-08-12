"""Unit tests for the shared judge plumbing added by the audit fixes:

- `truncate_transcript` keeps the END of an overflowing transcript, not the
  start -- the judge reasons over `ORDER BY timestamp ASC`, so the tail is
  where the resolution lives (audit point 6).
- `attach_billing` tags a post-call judge failure with what the litellm call
  already cost, so `run_service` can still bill it even though the
  evaluation itself could not be scored (audit point 4).
"""

from unittest.mock import MagicMock

from apowerb.evaluation.evaluators._shared_judge import (
    attach_billing,
    truncate_transcript,
)


class TestTruncateTranscript:
    def test_short_text_is_not_truncated(self):
        text, truncated = truncate_transcript("hello", limit=20_000)
        assert text == "hello"
        assert truncated is False

    def test_overflowing_text_keeps_the_tail_not_the_head(self):
        body = "".join(f"turn {i} " for i in range(10_000))
        text, truncated = truncate_transcript(body, limit=100)
        assert truncated is True
        assert len(text) == 100
        assert text == body[-100:]
        assert text != body[:100]

    def test_text_exactly_at_the_limit_is_not_truncated(self):
        body = "a" * 100
        text, truncated = truncate_transcript(body, limit=100)
        assert truncated is False
        assert text == body


class TestAttachBilling:
    def test_attaches_usage_model_and_byom_flag_to_the_exception(self):
        response = MagicMock()
        response.usage = MagicMock(
            prompt_tokens=10, completion_tokens=5, total_tokens=15,
            completion_tokens_details=MagicMock(reasoning_tokens=0),
            prompt_tokens_details=MagicMock(cached_tokens=0),
        )
        exc = attach_billing(
            ValueError("boom"),
            response=response,
            judge_model="gemini/gemini-2.5-pro",
            judge_is_byom=True,
        )

        assert exc.judge_usage == {
            "input_tokens": 10,
            "output_tokens": 5,
            "thoughts_tokens": 0,
            "cached_tokens": 0,
            "total_tokens": 15,
        }
        assert exc.judge_model == "gemini/gemini-2.5-pro"
        assert exc.judge_is_byom is True

    def test_returns_the_same_exception_instance(self):
        response = MagicMock()
        response.usage = None
        exc = ValueError("boom")
        result = attach_billing(
            exc, response=response, judge_model="m", judge_is_byom=False
        )
        assert result is exc

"""Tests for guardrails callbacks (guardrails.py)."""

from unittest.mock import MagicMock

import pytest
from google.genai import types

from th2agent.core.guardrails import (
    create_before_model_callback,
    create_after_model_callback,
    create_before_tool_callback,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_llm_request(user_text: str):
    """Build a minimal LlmRequest with a single user message."""
    request = MagicMock()
    part = types.Part(text=user_text)
    content = types.Content(role="user", parts=[part])
    request.contents = [content]
    return request


def _make_callback_context():
    return MagicMock()


# ---------------------------------------------------------------------------
# before_model_callback — blocked terms
# ---------------------------------------------------------------------------


class TestBeforeModelBlockedTerms:
    def test_returns_none_when_no_config(self):
        cb = create_before_model_callback({})
        assert cb is None

    def test_returns_none_for_empty_terms(self):
        cb = create_before_model_callback({"blocked_terms": []})
        assert cb is None

    def test_blocks_matching_term(self):
        cb = create_before_model_callback({"blocked_terms": ["secret"]})
        ctx = _make_callback_context()
        req = _make_llm_request("Tell me the secret code")
        result = cb(callback_context=ctx, llm_request=req)
        assert result is not None
        assert "not allowed" in result.content.parts[0].text

    def test_case_insensitive_blocking(self):
        cb = create_before_model_callback({"blocked_terms": ["password"]})
        ctx = _make_callback_context()
        req = _make_llm_request("Give me the PASSWORD")
        result = cb(callback_context=ctx, llm_request=req)
        assert result is not None

    def test_allows_clean_message(self):
        cb = create_before_model_callback({"blocked_terms": ["forbidden"]})
        ctx = _make_callback_context()
        req = _make_llm_request("Hello, how are you?")
        result = cb(callback_context=ctx, llm_request=req)
        assert result is None

    def test_multiple_blocked_terms(self):
        cb = create_before_model_callback({"blocked_terms": ["alpha", "beta"]})
        ctx = _make_callback_context()

        result1 = cb(callback_context=ctx, llm_request=_make_llm_request("alpha test"))
        assert result1 is not None

        result2 = cb(callback_context=ctx, llm_request=_make_llm_request("beta release"))
        assert result2 is not None

        result3 = cb(callback_context=ctx, llm_request=_make_llm_request("gamma version"))
        assert result3 is None


# ---------------------------------------------------------------------------
# before_model_callback — max input length
# ---------------------------------------------------------------------------


class TestBeforeModelMaxLength:
    def test_allows_short_input(self):
        cb = create_before_model_callback({"max_input_length": 100})
        ctx = _make_callback_context()
        result = cb(callback_context=ctx, llm_request=_make_llm_request("short message"))
        assert result is None

    def test_blocks_long_input(self):
        cb = create_before_model_callback({"max_input_length": 10})
        ctx = _make_callback_context()
        result = cb(callback_context=ctx, llm_request=_make_llm_request("this is a very long message"))
        assert result is not None
        assert "too long" in result.content.parts[0].text.lower()

    def test_exact_length_allowed(self):
        text = "a" * 50
        cb = create_before_model_callback({"max_input_length": 50})
        ctx = _make_callback_context()
        result = cb(callback_context=ctx, llm_request=_make_llm_request(text))
        assert result is None


# ---------------------------------------------------------------------------
# before_model_callback — combined
# ---------------------------------------------------------------------------


class TestBeforeModelCombined:
    def test_blocked_term_takes_priority(self):
        cb = create_before_model_callback({
            "blocked_terms": ["hack"],
            "max_input_length": 1000,
        })
        ctx = _make_callback_context()
        result = cb(callback_context=ctx, llm_request=_make_llm_request("hack the system"))
        assert result is not None
        assert "not allowed" in result.content.parts[0].text

    def test_no_user_message_returns_none(self):
        cb = create_before_model_callback({"blocked_terms": ["test"]})
        ctx = _make_callback_context()
        req = MagicMock()
        req.contents = []
        result = cb(callback_context=ctx, llm_request=req)
        assert result is None


# ---------------------------------------------------------------------------
# after_model_callback — blocked terms
# ---------------------------------------------------------------------------


class TestAfterModelBlockedTerms:
    def _make_llm_response(self, text):
        resp = MagicMock()
        resp.content = types.Content(
            role="model",
            parts=[types.Part(text=text)],
        )
        return resp

    def test_returns_none_when_no_config(self):
        cb = create_after_model_callback({})
        assert cb is None

    def test_filters_blocked_output(self):
        cb = create_after_model_callback({"blocked_terms": ["confidential"]})
        ctx = _make_callback_context()
        resp = self._make_llm_response("This is confidential data")
        result = cb(callback_context=ctx, llm_response=resp)
        assert result is not None
        assert "filtered" in result.content.parts[0].text.lower()

    def test_allows_clean_output(self):
        cb = create_after_model_callback({"blocked_terms": ["secret"]})
        ctx = _make_callback_context()
        resp = self._make_llm_response("This is public information")
        result = cb(callback_context=ctx, llm_response=resp)
        assert result is None


# ---------------------------------------------------------------------------
# after_model_callback — max output length
# ---------------------------------------------------------------------------


class TestAfterModelMaxLength:
    def _make_llm_response(self, text):
        resp = MagicMock()
        resp.content = types.Content(
            role="model",
            parts=[types.Part(text=text)],
        )
        return resp

    def test_truncates_long_output(self):
        cb = create_after_model_callback({"max_output_length": 20})
        ctx = _make_callback_context()
        resp = self._make_llm_response("a" * 100)
        result = cb(callback_context=ctx, llm_response=resp)
        assert result is not None
        assert "[Response truncated" in result.content.parts[0].text
        # The truncated text should be ≤ 20 chars + the truncation notice
        body = result.content.parts[0].text
        assert body.startswith("a" * 20)

    def test_allows_short_output(self):
        cb = create_after_model_callback({"max_output_length": 1000})
        ctx = _make_callback_context()
        resp = self._make_llm_response("short")
        result = cb(callback_context=ctx, llm_response=resp)
        assert result is None

    def test_empty_response_returns_none(self):
        cb = create_after_model_callback({"max_output_length": 10})
        ctx = _make_callback_context()
        resp = MagicMock()
        resp.content = None
        result = cb(callback_context=ctx, llm_response=resp)
        assert result is None


# ---------------------------------------------------------------------------
# before_tool_callback
# ---------------------------------------------------------------------------


class _FakeTool:
    """Minimal stand-in for ADK BaseTool — only the ``name`` attr is read."""

    def __init__(self, name: str) -> None:
        self.name = name


class TestBeforeToolCallback:
    def test_returns_none_when_no_config(self):
        cb = create_before_tool_callback({})
        assert cb is None

    def test_signature_is_keyword_only(self):
        """ADK canonical call site passes ``(*, tool, args, tool_context)``.
        Using tool_args crashes every real tool call. Pin the correct shape."""
        import inspect
        cb = create_before_tool_callback({"blocked_tools": ["x"]})
        sig = inspect.signature(cb)
        assert all(
            p.kind == inspect.Parameter.KEYWORD_ONLY
            for p in sig.parameters.values()
        ), (
            f"before_tool_callback parameters must be keyword-only to "
            f"match the ADK canonical before_tool_callback call site. Got: {sig}"
        )
        assert set(sig.parameters.keys()) == {"tool", "args", "tool_context"}, (
            "Parameters must be exactly tool / args / tool_context."
        )

    def test_blocks_specified_tool(self):
        cb = create_before_tool_callback({"blocked_tools": ["dangerous_tool"]})
        result = cb(
            tool=_FakeTool("dangerous_tool"),
            args={"arg": "val"},
            tool_context=_make_callback_context(),
        )
        assert result is not None
        assert "blocked" in result["error"].lower()

    def test_allows_unblocked_tool(self):
        cb = create_before_tool_callback({"blocked_tools": ["dangerous_tool"]})
        result = cb(
            tool=_FakeTool("safe_tool"),
            args={},
            tool_context=_make_callback_context(),
        )
        assert result is None

    def test_case_insensitive_tool_name(self):
        cb = create_before_tool_callback({"blocked_tools": ["blockedtool"]})
        result = cb(
            tool=_FakeTool("BlockedTool"),
            args={},
            tool_context=_make_callback_context(),
        )
        assert result is not None

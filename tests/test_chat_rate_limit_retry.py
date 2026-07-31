"""Tests for the chat-side rate-limit retry in ``stream_adk_agent``.

Live regression 2026-05-07 14:56 UTC: a Gemini 429 in the middle of a
chat turn surfaced as an SSE error chunk; the LLM saw no tool result for
its INSERT, hallucinated success, and claimed it had written into
SuiviAR. The DB stayed empty. PR #120 already handles this for the
webhook path via the backlog worker; this test suite covers the
interactive-chat counterpart.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from apowerb.core import adk_runner


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


_RATE_LIMIT_CHUNK = (
    'data: {"error": "litellm.RateLimitError: vertex_ai_betaException - '
    '{\\"error\\": {\\"code\\": 429, \\"status\\": \\"RESOURCE_EXHAUSTED\\", '
    '\\"details\\": [{\\"@type\\": \\"type.googleapis.com/google.rpc.RetryInfo\\", '
    '\\"retryDelay\\": \\"22s\\"}]}}"}\n\n'
)


def test_chunk_signals_rate_limit_detects_429():
    assert adk_runner._chunk_signals_rate_limit(_RATE_LIMIT_CHUNK)


def test_chunk_signals_rate_limit_detects_resource_exhausted():
    assert adk_runner._chunk_signals_rate_limit(
        'data: {"error":"RESOURCE_EXHAUSTED"}\n\n'
    )


def test_chunk_signals_rate_limit_ignores_normal_event():
    assert not adk_runner._chunk_signals_rate_limit(
        'data: {"content":{"role":"model","parts":[{"text":"hello"}]}}\n\n'
    )


def test_chunk_signals_rate_limit_ignores_empty():
    assert not adk_runner._chunk_signals_rate_limit("")
    assert not adk_runner._chunk_signals_rate_limit(None)


def test_parse_retry_delay_extracts_structured_value():
    assert adk_runner._parse_retry_delay_from_chunk(_RATE_LIMIT_CHUNK) == 22.0


def test_parse_retry_delay_extracts_human_readable_form():
    assert (
        adk_runner._parse_retry_delay_from_chunk(
            "litellm.RateLimitError: Please retry in 47.143958509s."
        )
        == pytest.approx(47.143958509)
    )


def test_parse_retry_delay_returns_none_when_absent():
    assert adk_runner._parse_retry_delay_from_chunk("just some text") is None


# --------------------------------------------------------------------------- #
# stream_adk_agent retry behaviour
# --------------------------------------------------------------------------- #


def _drain(async_gen):
    """Collect every chunk from an async generator into a list."""

    async def go():
        out = []
        async for chunk in async_gen:
            out.append(chunk)
        return out

    return asyncio.run(go())


async def _instant_sleep(seconds):
    """Stub for ``asyncio.sleep`` — returns immediately."""
    return None


def _patched_loop(monkeypatch):
    """Avoid eating real wall-clock seconds in tests by stubbing
    ``asyncio.sleep``."""
    monkeypatch.setattr(adk_runner.asyncio, "sleep", _instant_sleep)


def _gen_from(chunks):
    """Build an async generator yielding ``chunks`` one by one."""

    async def g(*args, **kwargs):
        for c in chunks:
            yield c

    return g


def test_no_rate_limit_passes_chunks_through(monkeypatch):
    success_stream = ['data: {"content":"hi"}\n\n', 'data: [DONE]\n\n']
    monkeypatch.setattr(
        adk_runner, "_stream_adk_agent_once", _gen_from(success_stream),
    )

    out = _drain(
        adk_runner.stream_adk_agent(
            agent_name="agent6",
            user_id="x",
            session_id="s",
            new_message={"role": "user", "parts": [{"text": "hi"}]},
            base_url="http://test",
            token="tk",
        )
    )
    assert out == success_stream


def test_single_rate_limit_then_success_yields_retry_marker(monkeypatch):
    _patched_loop(monkeypatch)

    calls = {"n": 0}

    async def fake_once(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            yield _RATE_LIMIT_CHUNK
        else:
            yield 'data: {"content":"ok"}\n\n'
            yield 'data: [DONE]\n\n'

    monkeypatch.setattr(adk_runner, "_stream_adk_agent_once", fake_once)

    out = _drain(
        adk_runner.stream_adk_agent(
            agent_name="agent6",
            user_id="x",
            session_id="s",
            new_message={"role": "user", "parts": []},
            base_url="http://test",
        )
    )
    assert calls["n"] == 2

    retry_markers = [c for c in out if '"info": "rate_limit_retry"' in c]
    assert len(retry_markers) == 1
    payload = json.loads(retry_markers[0].split("data: ", 1)[1].strip())
    assert payload["delay_seconds"] == 22.0
    assert payload["attempt"] == 1

    # Final success chunks present, original error chunk NOT forwarded
    assert any('"content":"ok"' in c for c in out)
    assert not any("RateLimitError" in c for c in out)


def test_retries_capped_then_emits_final_error(monkeypatch):
    _patched_loop(monkeypatch)

    monkeypatch.setattr(
        adk_runner, "_stream_adk_agent_once", _gen_from([_RATE_LIMIT_CHUNK]),
    )

    out = _drain(
        adk_runner.stream_adk_agent(
            agent_name="agent6", user_id="x", session_id="s",
            new_message={"role": "user", "parts": []},
            base_url="http://test",
        )
    )

    # First two attempts emit retry markers, third forwards the original
    # rate-limit chunk + a final "persisted" error.
    retry_markers = [c for c in out if '"info": "rate_limit_retry"' in c]
    assert len(retry_markers) == adk_runner._CHAT_MAX_RATE_LIMIT_RETRIES

    final_errors = [
        c for c in out if "Rate limit persisted" in c
    ]
    assert len(final_errors) == 1


def test_retry_delay_above_cap_forwards_error_immediately(monkeypatch):
    """If the provider asks for >60s, we don't keep the user waiting —
    we surface the original error and let them retry manually."""
    _patched_loop(monkeypatch)

    huge_delay_chunk = (
        'data: {"error":"RateLimitError ... \\"retryDelay\\": \\"120s\\""}\n\n'
    )
    monkeypatch.setattr(
        adk_runner, "_stream_adk_agent_once", _gen_from([huge_delay_chunk]),
    )

    out = _drain(
        adk_runner.stream_adk_agent(
            agent_name="agent6", user_id="x", session_id="s",
            new_message={"role": "user", "parts": []},
            base_url="http://test",
        )
    )

    # Original error forwarded, no retry marker
    assert any("RateLimitError" in c for c in out)
    assert not any('"info": "rate_limit_retry"' in c for c in out)


def test_chunks_before_rate_limit_are_yielded(monkeypatch):
    """If the LLM had time to emit some content before the 429, that
    partial content must reach the user (even though we then retry)."""
    _patched_loop(monkeypatch)

    calls = {"n": 0}

    async def fake_once(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            yield 'data: {"content":"partial"}\n\n'
            yield _RATE_LIMIT_CHUNK
        else:
            yield 'data: {"content":"final"}\n\n'

    monkeypatch.setattr(adk_runner, "_stream_adk_agent_once", fake_once)

    out = _drain(
        adk_runner.stream_adk_agent(
            agent_name="agent6", user_id="x", session_id="s",
            new_message={"role": "user", "parts": []},
            base_url="http://test",
        )
    )

    # The partial content from the failed attempt is in the output (the
    # frontend can decide to discard it on retry_marker arrival).
    assert any('"content":"partial"' in c for c in out)
    # Plus the final answer
    assert any('"content":"final"' in c for c in out)

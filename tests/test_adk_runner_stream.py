"""Unit tests for the SSE forwarding loop in core/adk_runner.stream_adk_agent.

The legacy implementation iterated `response.content` line by line, which
crashed with `Chunk too big` whenever ADK emitted a single SSE event larger
than aiohttp's per-line cap (~64 KB). The fix switched to `iter_any()` plus
an incremental UTF-8 decoder so that big tool payloads or long final
assistant texts pass through intact.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


class _FakeResponse:
    """Minimal stand-in for aiohttp's ClientResponse for SSE forwarding."""

    def __init__(self, *, status: int, content_type: str, chunks: list[bytes]):
        self.status = status
        self.headers = {"Content-Type": content_type}
        self.content = MagicMock()

        async def _iter_any():
            for c in chunks:
                yield c

        self.content.iter_any = _iter_any

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeSession:
    def __init__(self, response: _FakeResponse):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def post(self, *args, **kwargs):
        return self._response


@pytest.mark.asyncio
async def test_stream_forwards_chunk_larger_than_aiohttp_line_cap():
    """A single SSE event > 64 KB must pass through without crashing."""
    from apowerb.core import adk_runner

    big_payload = "x" * 200_000  # 200 KB — well above aiohttp's 64 KB readline cap
    sse = f"data: {{\"big\": \"{big_payload}\"}}\n\n".encode("utf-8")
    response = _FakeResponse(status=200, content_type="text/event-stream", chunks=[sse])

    with patch.object(adk_runner.aiohttp, "ClientSession", return_value=_FakeSession(response)):
        out = []
        async for piece in adk_runner.stream_adk_agent(
            agent_name="agent1",
            user_id="u",
            session_id="s",
            new_message={},
            base_url="http://fake",
        ):
            out.append(piece)

    joined = "".join(out)
    assert big_payload in joined
    assert joined.endswith("\n\n")


@pytest.mark.asyncio
async def test_stream_handles_utf8_split_across_chunks():
    """Multi-byte UTF-8 chars cut between chunks must not produce mojibake."""
    from apowerb.core import adk_runner

    full = "data: éàü\n\n".encode("utf-8")
    chunks = [full[:7], full[7:]]  # split in the middle of a multi-byte char
    response = _FakeResponse(status=200, content_type="text/event-stream", chunks=chunks)

    with patch.object(adk_runner.aiohttp, "ClientSession", return_value=_FakeSession(response)):
        out = []
        async for piece in adk_runner.stream_adk_agent(
            agent_name="agent1",
            user_id="u",
            session_id="s",
            new_message={},
            base_url="http://fake",
        ):
            out.append(piece)

    assert "".join(out) == "data: éàü\n\n"


@pytest.mark.asyncio
async def test_stream_names_a_provider_refusal_instead_of_forwarding_it():
    """Roadmap 37: after a model switch, the chat showed LiteLLM's raw text
    ("Please set ANTHROPIC_API_KEY in your environment vars"). The user gets
    the category and what to do; the provider text stays in the logs."""
    from apowerb.core import adk_runner

    raw = (
        'data: {"error": "AuthenticationError: litellm.AuthenticationError: Missing '
        'Anthropic API Key - Please set ANTHROPIC_API_KEY in your environment vars"}\n\n'
    )
    response = _FakeResponse(status=200, content_type="text/event-stream", chunks=[raw.encode()])

    with patch.object(adk_runner.aiohttp, "ClientSession", return_value=_FakeSession(response)):
        out = []
        async for piece in adk_runner.stream_adk_agent(
            agent_name="agent1",
            user_id="u",
            session_id="s",
            new_message={},
            base_url="http://fake",
        ):
            out.append(piece)

    joined = "".join(out)
    event = json.loads(joined.split("data: ", 1)[1].split("\n\n", 1)[0])
    assert event["code"] == "model_provider_auth"
    assert "conversation is kept" in event["error"]
    assert "ANTHROPIC_API_KEY" not in joined

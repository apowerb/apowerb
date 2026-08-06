"""The SSE stream must not hand exception text to the browser.

`_stream_adk_agent_once` had three yields that pushed raw error text into
the event stream: the body of a non-200 from the ADK server, and `str(e)`
from both the `ClientError` and the bare `Exception` handler. Whatever the
upstream runtime put in there -- a provider traceback, a connection string,
an internal host -- went straight to the client. CodeQL flagged the call
site (`py/stack-trace-exposure`); the leak is in the generator behind it.

Sanitizing this envelope is not a free replacement, because two consumers
read it and both break on an opaque string:

  - the retry wrapper recognises a rate limit by pattern-matching the
    forwarded chunk (`_chunk_signals_rate_limit`);
  - the frontend tests the message for "session not found" to tell the user
    their conversation state is gone, instead of showing a raw error
    (th2agent-app `src/hooks/useStreaming.js`).

So these tests pin both halves: nothing sensitive leaves, and the two
signals downstream still arrive.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


class _FakeResponse:
    """aiohttp ClientResponse stand-in, with a body for the non-200 path."""

    def __init__(self, *, status: int, body: str = "", content_type: str = "text/plain"):
        self.status = status
        self.headers = {"Content-Type": content_type}
        self._body = body
        self.content = MagicMock()

        async def _iter_any():
            return
            yield  # pragma: no cover -- makes this an async generator

        self.content.iter_any = _iter_any

    async def text(self) -> str:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeSession:
    def __init__(self, response=None, raise_on_post: BaseException | None = None):
        self._response = response
        self._raise = raise_on_post

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def post(self, *args, **kwargs):
        if self._raise is not None:
            raise self._raise
        return self._response


async def _collect(session, **overrides):
    from apowerb.core import adk_runner

    kwargs = {
        "agent_name": "agent1",
        "user_id": "u",
        "session_id": "s",
        "new_message": {},
        "base_url": "http://fake",
    }
    kwargs.update(overrides)

    with patch.object(adk_runner.aiohttp, "ClientSession", return_value=session):
        return [piece async for piece in adk_runner.stream_adk_agent(**kwargs)]


def _events(chunks: list[str]) -> list[dict]:
    """Parse the JSON payloads out of collected SSE chunks."""
    out = []
    for chunk in chunks:
        for line in chunk.splitlines():
            if not line.startswith("data: "):
                continue
            payload = line[6:]
            if payload == "[DONE]":
                continue
            try:
                out.append(json.loads(payload))
            except json.JSONDecodeError:
                pass
    return out


TRACEBACK_BODY = (
    'Traceback (most recent call last):\n'
    '  File "/opt/adk/google/adk/cli/api_server.py", line 214, in run_sse\n'
    "    raise RuntimeError(dsn)\n"
    "RuntimeError: postgresql://th2:hunter2@10.0.0.7:5432/agents\n"
)


@pytest.mark.asyncio
async def test_a_traceback_body_never_reaches_the_client():
    """A 500 from the ADK server must not forward its body."""
    response = _FakeResponse(status=500, body=TRACEBACK_BODY)
    chunks = await _collect(_FakeSession(response))

    joined = "".join(chunks)
    assert "Traceback" not in joined
    assert "hunter2" not in joined
    assert "10.0.0.7" not in joined
    assert "api_server.py" not in joined

    events = _events(chunks)
    assert events, "the client must still be told something went wrong"
    assert events[0]["error"]
    assert events[0]["status"] == 500


@pytest.mark.asyncio
async def test_the_lost_session_case_keeps_the_wording_the_frontend_matches():
    """The frontend turns "session not found" into actionable guidance.

    Replacing every non-200 body with one generic message would silently
    downgrade that to a raw error banner, so this case keeps a message of
    its own -- without the session id the upstream body carries.
    """
    response = _FakeResponse(
        status=404, body='{"detail":"Session not found: test-quota-68273"}'
    )
    chunks = await _collect(_FakeSession(response))

    joined = "".join(chunks)
    assert "session not found" in joined.lower(), "frontend branch would break"
    assert "test-quota-68273" not in joined, "the session id is upstream detail"


@pytest.mark.asyncio
async def test_a_rate_limit_status_stays_recognisable_to_the_retry_wrapper():
    """`_chunk_signals_rate_limit` reads the envelope, so it must survive.

    A 429 arriving as an HTTP status rather than inside the stream is rare
    -- 14 days of dev logs show only 404 and 500 on this path -- but if the
    envelope stopped carrying the code, the retry would silently stop
    firing and the user would get the error the retry exists to hide.
    """
    from apowerb.core import adk_runner

    response = _FakeResponse(status=429, body="rate limited, retry later")
    chunks = await _collect(_FakeSession(response))

    assert any(adk_runner._chunk_signals_rate_limit(c) for c in chunks)


@pytest.mark.asyncio
async def test_a_connection_error_does_not_leak_its_message():
    """`str(ClientError)` embeds the host and port we failed to reach."""
    import aiohttp

    from apowerb.core import adk_runner

    boom = aiohttp.ClientConnectorError(
        connection_key=MagicMock(ssl=None, host="adk-internal.thaink2.local", port=8003),
        os_error=OSError(111, "Connection refused"),
    )
    chunks = await _collect(_FakeSession(raise_on_post=boom))

    joined = "".join(chunks)
    assert "adk-internal.thaink2.local" not in joined
    assert "Connection refused" not in joined

    events = _events(chunks)
    assert events and events[0]["error"] == adk_runner._UPSTREAM_ERROR_MESSAGE


@pytest.mark.asyncio
async def test_an_unexpected_exception_does_not_leak_its_message():
    """The bare `except Exception` is the widest surface of the three."""
    from apowerb.core import adk_runner

    boom = RuntimeError("ENCRYPT_KEY is not configured at /srv/th2/.env")
    chunks = await _collect(_FakeSession(raise_on_post=boom))

    joined = "".join(chunks)
    assert "ENCRYPT_KEY" not in joined
    assert "/srv/th2/.env" not in joined

    events = _events(chunks)
    assert events and events[0]["error"] == adk_runner._UPSTREAM_ERROR_MESSAGE


@pytest.mark.asyncio
async def test_the_full_detail_still_reaches_the_operator(caplog):
    """Sanitizing the client's copy must not sanitize ours."""
    import logging

    response = _FakeResponse(status=500, body=TRACEBACK_BODY)
    with caplog.at_level(logging.ERROR, logger="apowerb.core.adk_runner"):
        await _collect(_FakeSession(response))

    assert "hunter2" in caplog.text, "operators lost the only copy of the cause"

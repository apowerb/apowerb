"""A voice session may only load an agent its user owns.

``start_listening`` names the agent; its persona, knowledge and tools belong
to that agent's owner. The websocket authenticates the user from the token,
so the named agent must be checked against that user, as every other route
taking an ``agent_id`` does (``helpers.ownership``).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from apowerb.routers.audio_stream import router

OWNER = "alice@a.test"
OTHER = "bob@b.test"


class _Row:
    def __init__(self, **values):
        self._values = values

    def _asdict(self):
        return dict(self._values)


def _tool():
    return "ok"


@pytest.fixture()
def voice(monkeypatch):
    """Gemini voice path with agent1 owned by OWNER; returns the doubles."""
    from apowerb.core import agent_helpers
    from apowerb.core.agent_main import agent_store
    from apowerb.tools_store import tools_helpers

    details = {
        "owner_id": OWNER,
        "organization_id": "org-a",
        "project_id": "pa",
        "agent_tools": "[]",
        "agent_instruction": "persona",
    }
    monkeypatch.setattr(agent_helpers, "get_agent_details", lambda agent_id, **_: dict(details))
    # AgentStore is a pydantic model: patch the method on its class.
    monkeypatch.setattr(
        type(agent_store),
        "get_list_agents",
        lambda self, query: [_Row(agent_id=1, owner_id=OWNER)],
    )
    load_tools = MagicMock(return_value=(["tool"], [_tool]))
    monkeypatch.setattr(tools_helpers, "load_agent_tools_functions", load_tools)

    session = AsyncMock()
    app = FastAPI()
    app.include_router(router, prefix="/api")
    with patch("apowerb.routers.audio_stream._use_gemini", return_value=True), patch(
        "apowerb.routers.audio_stream.GeminiLiveSession", return_value=session
    ), patch("apowerb.routers.audio_stream.jwt") as mock_jwt:
        yield SimpleNamespace(
            client=TestClient(app), jwt=mock_jwt, session=session, load_tools=load_tools
        )


def _connect(voice, email):
    voice.jwt.decode.return_value = {"sub": email}
    return voice.client.websocket_connect("/api/audio/ws/s-1?token=valid")


def test_owner_gets_the_agent_and_its_tools(voice):
    with _connect(voice, OWNER) as ws:
        ws.send_json({"type": "start_listening", "agent_name": "agent1"})
        assert ws.receive_json()["type"] == "listening_started"

    kwargs = voice.session.start.await_args.kwargs
    assert kwargs["tools"] == [_tool]
    assert "persona" in kwargs["system_instruction"]


def test_agent_of_another_owner_is_refused(voice):
    with _connect(voice, OTHER) as ws:
        ws.send_json({"type": "start_listening", "agent_name": "agent1"})
        assert ws.receive_json()["type"] == "error"
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    assert closed.value.code == 4003

    voice.session.start.assert_not_awaited()
    voice.load_tools.assert_not_called()


def test_session_without_agent_is_unchanged(voice):
    with _connect(voice, OTHER) as ws:
        ws.send_json({"type": "start_listening"})
        assert ws.receive_json()["type"] == "listening_started"

    kwargs = voice.session.start.await_args.kwargs
    assert kwargs["tools"] == []
    assert kwargs["system_instruction"] is None

"""Tests for the ?mode= query param on /audio/ws/{session_id}.

- mode=dictation: WS must accept, send an error telling the client to use
  Web Speech API, then close cleanly.
- mode=conversation (default): behaves like before (ping/pong works).
"""

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from th2agent.routers.audio_stream import router


def _create_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix="/api")
    return app


def _payload(email: str = "test@example.com") -> dict:
    return {"sub": email}


class TestDictationMode:
    def test_dictation_mode_rejects_with_error_and_closes(self) -> None:
        app = _create_app()
        client = TestClient(app)
        with patch("th2agent.routers.audio_stream.jwt") as mock_jwt:
            mock_jwt.decode.return_value = _payload()
            with client.websocket_connect(
                "/api/audio/ws/s-1?token=valid&mode=dictation"
            ) as ws:
                msg = ws.receive_json()
                assert msg["type"] == "error"
                assert "Web Speech API" in msg["message"]
                with pytest.raises(WebSocketDisconnect):
                    ws.receive_json()

    def test_conversation_mode_is_default(self) -> None:
        app = _create_app()
        client = TestClient(app)
        with patch("th2agent.routers.audio_stream.jwt") as mock_jwt:
            mock_jwt.decode.return_value = _payload()
            with client.websocket_connect("/api/audio/ws/s-1?token=valid") as ws:
                ws.send_json({"type": "ping"})
                resp = ws.receive_json()
                assert resp["type"] == "pong"

    def test_explicit_conversation_mode_behaves_normally(self) -> None:
        app = _create_app()
        client = TestClient(app)
        with patch("th2agent.routers.audio_stream.jwt") as mock_jwt:
            mock_jwt.decode.return_value = _payload()
            with client.websocket_connect(
                "/api/audio/ws/s-1?token=valid&mode=conversation"
            ) as ws:
                ws.send_json({"type": "ping"})
                resp = ws.receive_json()
                assert resp["type"] == "pong"


class TestLanguagePropagation:
    def test_start_listening_propagates_language_to_session(self) -> None:
        """The language from start_listening must reach GeminiLiveSession.start."""
        from unittest.mock import AsyncMock

        app = _create_app()
        client = TestClient(app)

        fake_session = AsyncMock()
        fake_session.start = AsyncMock()
        fake_session.stop = AsyncMock()

        with patch("th2agent.routers.audio_stream.jwt") as mock_jwt, \
             patch("th2agent.routers.audio_stream._use_gemini", return_value=True), \
             patch(
                 "th2agent.routers.audio_stream.GeminiLiveSession",
                 return_value=fake_session,
             ):
            mock_jwt.decode.return_value = _payload()
            with client.websocket_connect("/api/audio/ws/s-1?token=valid") as ws:
                ws.send_json({
                    "type": "start_listening",
                    "language": "fr-FR",
                    "voice": "Kore",
                })
                resp = ws.receive_json()
                assert resp["type"] == "listening_started"

        assert fake_session.start.await_count == 1
        kwargs = fake_session.start.await_args.kwargs
        assert kwargs.get("language") == "fr-FR"

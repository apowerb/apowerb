"""Tests for routers/audio_stream.py — WebSocket audio streaming endpoint."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apowerb.routers.audio_stream import router


def _create_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix="/api")
    return app


def _make_token_payload(email: str = "test@example.com") -> dict:
    return {"sub": email}


class TestWebSocketAuth:
    def test_rejects_connection_without_token(self) -> None:
        app = _create_app()
        client = TestClient(app)
        with pytest.raises(Exception):
            with client.websocket_connect("/api/audio/ws/session-1"):
                pass

    def test_rejects_connection_with_invalid_token(self) -> None:
        app = _create_app()
        client = TestClient(app)
        with pytest.raises(Exception):
            with client.websocket_connect("/api/audio/ws/session-1?token=invalid-jwt"):
                pass

    def test_accepts_connection_with_valid_token(self) -> None:
        app = _create_app()
        client = TestClient(app)

        with patch("apowerb.routers.audio_stream.jwt") as mock_jwt:
            mock_jwt.decode.return_value = _make_token_payload()
            with client.websocket_connect("/api/audio/ws/session-1?token=valid-jwt") as ws:
                ws.send_json({"type": "ping"})
                response = ws.receive_json()
                assert response["type"] == "pong"


class TestWebSocketProtocol:
    def _connect(self, client: TestClient, session_id: str = "test-session"):
        return client.websocket_connect(f"/api/audio/ws/{session_id}?token=valid")

    @pytest.fixture()
    def patched_app(self):
        app = _create_app()
        client = TestClient(app)
        with patch("apowerb.routers.audio_stream.jwt") as mock_jwt:
            mock_jwt.decode.return_value = _make_token_payload()
            yield client

    def test_ping_pong(self, patched_app: TestClient) -> None:
        with self._connect(patched_app) as ws:
            ws.send_json({"type": "ping"})
            resp = ws.receive_json()
            assert resp["type"] == "pong"

    def test_unknown_message_type_returns_error(self, patched_app: TestClient) -> None:
        with self._connect(patched_app) as ws:
            ws.send_json({"type": "unknown_command"})
            resp = ws.receive_json()
            assert resp["type"] == "error"
            assert "Unknown" in resp["message"] or "unknown" in resp["message"]

    def test_start_listening_returns_confirmation(self, patched_app: TestClient) -> None:
        with patch("apowerb.routers.audio_stream.FallbackSTT") as mock_stt_cls:
            mock_stt = AsyncMock()
            mock_stt_cls.return_value = mock_stt
            with self._connect(patched_app) as ws:
                ws.send_json({
                    "type": "start_listening",
                    "language": "en",
                    "provider": "auto",
                })
                resp = ws.receive_json()
                assert resp["type"] == "listening_started"

    def test_stop_listening_returns_confirmation(self, patched_app: TestClient) -> None:
        with patch("apowerb.routers.audio_stream.FallbackSTT") as mock_stt_cls:
            mock_stt = AsyncMock()
            mock_stt_cls.return_value = mock_stt
            with self._connect(patched_app) as ws:
                ws.send_json({
                    "type": "start_listening",
                    "language": "auto",
                    "provider": "auto",
                })
                ws.receive_json()

                ws.send_json({"type": "stop_listening"})
                resp = ws.receive_json()
                assert resp["type"] == "listening_stopped"

    def test_start_speaking_triggers_tts(self, patched_app: TestClient) -> None:
        with patch("apowerb.routers.audio_stream.FallbackTTS") as mock_tts_cls:
            mock_tts = AsyncMock()
            mock_tts.stream_tts = AsyncMock()
            mock_tts_cls.return_value = mock_tts
            with self._connect(patched_app) as ws:
                ws.send_json({
                    "type": "start_speaking",
                    "text": "Hello world",
                    "voice": "alloy",
                    "provider": "auto",
                })
                resp = ws.receive_json()
                assert resp["type"] == "tts_start"

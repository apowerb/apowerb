"""Tests for core/audio_streaming.py — FallbackSTT and FallbackTTS classes."""

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator
from urllib.parse import parse_qs, urlparse
from unittest.mock import AsyncMock, patch

import pytest

from apowerb.core.audio_streaming import FallbackSTT, FallbackTTS


class _FakeStreamResponse:
    """Fake httpx streaming response for TTS tests."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.status_code = 200

    def raise_for_status(self) -> None:
        pass

    def aiter_bytes(self, chunk_size: int = 4096) -> AsyncIterator[bytes]:
        return self._aiter(self._chunks)

    @staticmethod
    async def _aiter(items: list[bytes]) -> AsyncIterator[bytes]:
        for item in items:
            yield item


class _FakeAsyncClient:
    """Fake httpx.AsyncClient that yields a FakeStreamResponse."""

    def __init__(self, response: _FakeStreamResponse) -> None:
        self._response = response

    @asynccontextmanager
    async def stream(self, method: str, url: str, **kwargs: object) -> AsyncIterator[_FakeStreamResponse]:
        yield self._response

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        pass


class TestFallbackSTT:
    def test_init_sets_defaults(self) -> None:
        stt = FallbackSTT()
        assert stt._ws is None
        assert stt._running is False
        assert stt._callback is None

    @pytest.mark.asyncio
    async def test_start_opens_deepgram_ws_when_key_available(self) -> None:
        callback = AsyncMock()
        mock_ws = AsyncMock()
        mock_ws.recv = AsyncMock(side_effect=asyncio.CancelledError)
        mock_ws.close = AsyncMock()

        with patch.dict("os.environ", {"DEEPGRAM_API_KEY": "test-key"}):
            with patch("websockets.connect", new_callable=AsyncMock) as mock_connect:
                mock_connect.return_value = mock_ws

                stt = FallbackSTT()
                await stt.start(language="en", on_transcript_callback=callback)

                assert stt._running is True
                assert stt._callback is callback
                mock_connect.assert_called_once()
                call_url = mock_connect.call_args[0][0]
                # Assert on the parsed host, not on a substring of the URL.
                # "api.deepgram.com" in call_url also passes for
                # wss://api.deepgram.com.evil.test/ and for a URL that merely
                # mentions the host in a query parameter, so the substring form
                # would not catch the redirection it looks like it is guarding.
                parsed = urlparse(call_url)
                assert parsed.hostname == "api.deepgram.com"
                assert parsed.scheme == "wss"
                assert "model=nova-2" in parse_qs(parsed.query).get("model", []) or                     parsed.query.count("model=nova-2") == 1

    @pytest.mark.asyncio
    async def test_start_without_deepgram_key_uses_fallback(self) -> None:
        callback = AsyncMock()

        env_copy = dict(__import__("os").environ)
        env_copy.pop("DEEPGRAM_API_KEY", None)
        with patch.dict("os.environ", env_copy, clear=True):
            stt = FallbackSTT()
            await stt.start(language="en", on_transcript_callback=callback)

            assert stt._running is True
            assert stt._fallback_mode is True

    @pytest.mark.asyncio
    async def test_send_audio_forwards_to_deepgram(self) -> None:
        stt = FallbackSTT()
        mock_ws = AsyncMock()
        stt._ws = mock_ws
        stt._running = True
        stt._fallback_mode = False

        chunk = b"\x00\x01\x02\x03"
        await stt.send_audio(chunk)

        mock_ws.send.assert_called_once_with(chunk)

    @pytest.mark.asyncio
    async def test_send_audio_accumulates_in_fallback_mode(self) -> None:
        stt = FallbackSTT()
        stt._running = True
        stt._fallback_mode = True
        stt._fallback_buffer = bytearray()

        chunk = b"\x00\x01\x02\x03"
        await stt.send_audio(chunk)

        assert bytes(stt._fallback_buffer) == chunk

    @pytest.mark.asyncio
    async def test_stop_sends_close_and_resets(self) -> None:
        stt = FallbackSTT()
        mock_ws = AsyncMock()
        stt._ws = mock_ws
        stt._running = True
        stt._fallback_mode = False
        stt._listen_task = None

        await stt.stop()

        assert stt._running is False
        mock_ws.send.assert_called_once()
        mock_ws.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_audio_noop_when_not_running(self) -> None:
        stt = FallbackSTT()
        stt._running = False
        await stt.send_audio(b"\x00")


class TestFallbackTTS:
    def test_init(self) -> None:
        tts = FallbackTTS()
        assert tts is not None

    @pytest.mark.asyncio
    async def test_stream_tts_openai_calls_api(self) -> None:
        chunks_received: list[bytes] = []

        async def on_chunk(chunk: bytes) -> None:
            chunks_received.append(chunk)

        fake_response = _FakeStreamResponse([b"chunk1", b"chunk2"])
        fake_client = _FakeAsyncClient(fake_response)

        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}):
            with patch("httpx.AsyncClient", return_value=fake_client):
                tts = FallbackTTS()
                await tts.stream_tts(
                    text="Hello world",
                    voice="alloy",
                    provider="openai",
                    on_chunk_callback=on_chunk,
                )

        assert len(chunks_received) == 2
        assert chunks_received[0] == b"chunk1"

    @pytest.mark.asyncio
    async def test_stream_tts_elevenlabs_calls_api(self) -> None:
        chunks_received: list[bytes] = []

        async def on_chunk(chunk: bytes) -> None:
            chunks_received.append(chunk)

        fake_response = _FakeStreamResponse([b"audio1", b"audio2", b"audio3"])
        fake_client = _FakeAsyncClient(fake_response)

        with patch.dict("os.environ", {"ELEVENLABS_API_KEY": "test-key"}):
            with patch("httpx.AsyncClient", return_value=fake_client):
                tts = FallbackTTS()
                await tts.stream_tts(
                    text="Bonjour",
                    voice="nova",
                    provider="elevenlabs",
                    on_chunk_callback=on_chunk,
                )

        assert len(chunks_received) == 3

    @pytest.mark.asyncio
    async def test_stream_tts_auto_fallback(self) -> None:
        chunks_received: list[bytes] = []

        async def on_chunk(chunk: bytes) -> None:
            chunks_received.append(chunk)

        fake_response = _FakeStreamResponse([b"data"])
        fake_client = _FakeAsyncClient(fake_response)

        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}, clear=False):
            with patch("httpx.AsyncClient", return_value=fake_client):
                tts = FallbackTTS()
                await tts.stream_tts(
                    text="Test",
                    voice="alloy",
                    provider="auto",
                    on_chunk_callback=on_chunk,
                )

        assert len(chunks_received) >= 1

    @pytest.mark.asyncio
    async def test_stream_tts_raises_when_no_provider(self) -> None:
        async def on_chunk(chunk: bytes) -> None:
            pass

        env = dict(__import__("os").environ)
        env.pop("OPENAI_API_KEY", None)
        env.pop("ELEVENLABS_API_KEY", None)

        with patch.dict("os.environ", env, clear=True):
            tts = FallbackTTS()
            with pytest.raises(RuntimeError, match="No TTS provider available"):
                await tts.stream_tts(
                    text="Test",
                    voice="alloy",
                    provider="auto",
                    on_chunk_callback=on_chunk,
                )

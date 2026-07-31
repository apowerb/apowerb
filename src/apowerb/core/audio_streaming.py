"""Real-time audio streaming via Gemini Live API (unified STT + TTS).

Fallback to Deepgram STT + OpenAI/ElevenLabs TTS when GEMINI_API_KEY is not set.
"""

import asyncio
import inspect
import json
import os
from logging import getLogger
from typing import Any, Awaitable, Callable

import httpx

logger = getLogger(__name__)

TranscriptCallback = Callable[[str, bool], Awaitable[None]]
AudioChunkCallback = Callable[[bytes], Awaitable[None]]

GEMINI_LIVE_MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"
GEMINI_VOICES = {"Puck", "Charon", "Kore", "Fenrir", "Aoede"}
DEFAULT_VOICE = "Kore"


_PY_TO_SCHEMA_TYPE: dict[Any, str] = {
    str: "STRING", int: "INTEGER", float: "NUMBER", bool: "BOOLEAN",
    list: "ARRAY", dict: "OBJECT",
}


def _build_function_declaration(fn: Callable[..., Any]) -> dict[str, Any]:
    """Introspect a Python callable and build a Gemini FunctionDeclaration dict.

    The SDK's auto-introspection is not available in Live mode, so we derive
    parameter schemas from the function signature and its docstring (first
    paragraph only, truncated).
    """
    name = getattr(fn, "__name__", "tool").split(".")[-1]
    doc = (inspect.getdoc(fn) or "").strip()
    description = doc.split("\n\n")[0][:500] if doc else f"Tool {name}"
    sig = inspect.signature(fn)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for pname, param in sig.parameters.items():
        if pname in ("self", "cls") or param.kind in (
            inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD,
        ):
            continue
        ann = param.annotation
        schema_type = "STRING"
        item_type: str | None = None
        origin = getattr(ann, "__origin__", None)
        args = getattr(ann, "__args__", ()) or ()
        if ann is inspect.Parameter.empty:
            schema_type = "STRING"
        elif ann in _PY_TO_SCHEMA_TYPE:
            schema_type = _PY_TO_SCHEMA_TYPE[ann]
        elif origin in (list, tuple):
            schema_type = "ARRAY"
            inner = args[0] if args else str
            item_type = _PY_TO_SCHEMA_TYPE.get(inner, "STRING")
        elif origin in (dict,):
            schema_type = "OBJECT"
        prop: dict[str, Any] = {"type": schema_type, "description": f"Parameter {pname}"}
        if schema_type == "ARRAY":
            prop["items"] = {"type": item_type or "STRING"}
        properties[pname] = prop
        if param.default is inspect.Parameter.empty:
            required.append(pname)
    decl: dict[str, Any] = {"name": name, "description": description}
    if properties:
        decl["parameters"] = {
            "type": "OBJECT",
            "properties": properties,
            **({"required": required} if required else {}),
        }
    return decl


class GeminiLiveSession:
    """Unified STT+TTS session via Gemini Live API.

    Sends PCM 16-bit 16kHz audio in, receives PCM 16-bit 24kHz audio out,
    plus text transcriptions in both directions.
    """

    def __init__(self) -> None:
        self._session = None
        self._session_cm = None
        self._running: bool = False
        self._receive_task: asyncio.Task | None = None
        self._on_transcript: TranscriptCallback | None = None
        self._on_audio_chunk: AudioChunkCallback | None = None
        self._on_input_transcript: TranscriptCallback | None = None
        self._on_tts_start: "Callable[[], Awaitable[None]] | None" = None
        self._on_tts_end: "Callable[[], Awaitable[None]] | None" = None
        self._tts_started_for_turn: bool = False
        self._model_speaking: bool = False
        self._input_transcript_buffer: str = ""
        self._output_transcript_buffer: str = ""
        # Gemini Live v1beta multi-turn is broken on the current preview models:
        # after turn_complete, the session ignores new audio. Workaround is to
        # close the session and reopen with session_resumption.handle to keep
        # conversation context. These fields track the cycle.
        self._resumption_handle: str | None = None
        self._client = None
        self._voice: str = DEFAULT_VOICE
        self._language: str = "auto"
        self._system_instruction: str | None = None
        self._reopening: bool = False
        # Conversation history kept across session reopens so the model retains
        # context even when Gemini doesn't return a resumption handle.
        # Each entry: {"role": "user"|"model", "text": str}
        self._history: list[dict[str, str]] = []
        self._max_history_turns: int = 20
        self._tool_funcs: dict[str, Callable[..., Any]] = {}
        self._tool_declarations: list[dict[str, Any]] = []

    async def start(
        self,
        voice: str = DEFAULT_VOICE,
        language: str = "auto",
        system_instruction: str | None = None,
        on_transcript: TranscriptCallback | None = None,
        on_audio_chunk: AudioChunkCallback | None = None,
        on_input_transcript: TranscriptCallback | None = None,
        on_state: "Callable[[str], Awaitable[None]] | None" = None,
        on_tts_start: "Callable[[], Awaitable[None]] | None" = None,
        on_tts_end: "Callable[[], Awaitable[None]] | None" = None,
        tools: list[Callable[..., Any]] | None = None,
    ) -> None:
        from google import genai
        from google.genai import types

        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise EnvironmentError("GEMINI_API_KEY not set")

        self._on_transcript = on_transcript
        self._on_audio_chunk = on_audio_chunk
        self._on_input_transcript = on_input_transcript
        self._on_state = on_state
        self._on_tts_start = on_tts_start
        self._on_tts_end = on_tts_end
        self._tts_started_for_turn = False
        self._input_transcript_buffer = ""
        self._output_transcript_buffer = ""
        self._state = "listening"
        self._running = True
        self._resumption_handle = None
        self._history = []

        if voice not in GEMINI_VOICES:
            voice = DEFAULT_VOICE
        self._voice = voice
        self._language = language
        self._system_instruction = system_instruction
        self._client = genai.Client(api_key=api_key)
        self._tool_funcs = {}
        self._tool_declarations = []
        if tools:
            for fn in tools:
                try:
                    decl = _build_function_declaration(fn)
                except Exception as exc:
                    logger.warning("[gemini] skipping tool %s: %s", getattr(fn, "__name__", fn), exc)
                    continue
                self._tool_funcs[decl["name"]] = fn
                self._tool_declarations.append(decl)
            logger.info("[gemini] registered %d tool(s) for voice session: %s",
                        len(self._tool_declarations), list(self._tool_funcs.keys()))

        await self._open_session()
        logger.info("Gemini Live session started (voice=%s)", voice)

    async def _open_session(self) -> None:
        """Open (or reopen with handle) a Gemini Live session."""
        from google.genai import types

        speech_kwargs: dict = {
            "voice_config": types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self._voice),
            ),
        }
        if self._language and self._language != "auto":
            speech_kwargs["language_code"] = self._language

        config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(**speech_kwargs),
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    silence_duration_ms=1500,
                    prefix_padding_ms=300,
                    start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_HIGH,
                    end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_LOW,
                ),
            ),
            session_resumption=types.SessionResumptionConfig(handle=self._resumption_handle),
        )
        if self._tool_declarations:
            config.tools = [
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(**d) for d in self._tool_declarations
                    ]
                )
            ]
        instruction_parts: list[str] = []
        instruction_parts.append(
            "## Voice mode rules (always apply)\n"
            "You are in a voice conversation. Speech-to-text introduces errors, "
            "especially on names, emails, codes, IDs, URLs, and technical terms.\n"
            "Whenever the user mentions anything that must be literal and precise — "
            "a name, an email address, a phone number, a reference code, a URL, "
            "a SKU, or any identifier — and you have any doubt about the exact "
            "spelling, you MUST NOT guess. Instead, ask the user to type it using "
            "the keyboard button in the voice UI. Use one of these exact phrasings "
            "so the interface can auto-open the text input:\n"
            "- French: \"Utilise le bouton clavier pour taper le nom/email exact.\"\n"
            "- English: \"Please use the keyboard button to type the exact name/email.\"\n"
            "- Italian: \"Usa il pulsante tastiera per scrivere il nome/email esatto.\"\n"
            "Do not send emails, call tools, or store data based on a phonetically "
            "unclear input. Precision beats speed.\n"
            "Also: remember every action you take during this conversation "
            "(emails sent, searches done, items created). If the user asks you to "
            "recall or repeat, consult your own previous turns — never claim "
            "nothing has happened yet when it has."
        )
        if self._system_instruction:
            instruction_parts.append(self._system_instruction)
        if self._history and not self._resumption_handle:
            recent = self._history[-self._max_history_turns * 2 :]
            transcript_block = "\n".join(
                f"{'User' if e['role'] == 'user' else 'Assistant'}: {e['text']}" for e in recent
            )
            instruction_parts.append(
                "Below is the prior conversation with this user. "
                "Continue naturally as the same Assistant — do not greet again, "
                "do not reintroduce yourself, and use this context to answer.\n\n"
                f"--- Conversation so far ---\n{transcript_block}\n--- End of history ---"
            )
        if instruction_parts:
            config.system_instruction = "\n\n".join(instruction_parts)

        self._session_cm = self._client.aio.live.connect(
            model=GEMINI_LIVE_MODEL,
            config=config,
        )
        self._session = await self._session_cm.__aenter__()
        self._receive_task = asyncio.create_task(self._receive_loop())

    async def _reopen_session(self) -> None:
        """Close current Gemini session and reopen with resumption handle."""
        if self._reopening:
            return
        self._reopening = True
        try:
            logger.info("[gemini] reopening session (handle=%s)", bool(self._resumption_handle))
            if self._receive_task is not None:
                self._receive_task.cancel()
                try:
                    await self._receive_task
                except asyncio.CancelledError:
                    pass
                self._receive_task = None
            if self._session is not None and self._session_cm is not None:
                try:
                    await self._session_cm.__aexit__(None, None, None)
                except Exception as exc:
                    logger.warning("[gemini] close during reopen: %s", exc)
                self._session = None
                self._session_cm = None
            if self._running:
                await self._open_session()
        finally:
            self._reopening = False

    async def _receive_loop(self) -> None:
        if self._session is None:
            return
        try:
            async for response in self._session.receive():
                if not self._running:
                    break

                resume_update = getattr(response, "session_resumption_update", None)
                if resume_update and resume_update.new_handle:
                    is_first = self._resumption_handle is None
                    self._resumption_handle = resume_update.new_handle
                    if is_first:
                        logger.info("[gemini] received first resumption handle (len=%d)", len(resume_update.new_handle))

                tool_call = getattr(response, "tool_call", None)
                if tool_call and getattr(tool_call, "function_calls", None):
                    await self._handle_tool_call(tool_call.function_calls)
                    continue

                server_content = response.server_content
                if server_content is None:
                    continue

                if server_content.input_transcription and server_content.input_transcription.text:
                    if self._state != "listening":
                        self._state = "listening"
                        if self._on_state:
                            await self._on_state("listening")
                    self._input_transcript_buffer += server_content.input_transcription.text
                    if self._on_input_transcript:
                        await self._on_input_transcript(
                            self._input_transcript_buffer, False
                        )

                if server_content.model_turn or server_content.output_transcription:
                    if self._state != "thinking":
                        self._state = "thinking"
                        if self._on_state:
                            await self._on_state("thinking")

                if server_content.output_transcription and server_content.output_transcription.text:
                    self._output_transcript_buffer += server_content.output_transcription.text
                    if self._on_transcript:
                        await self._on_transcript(
                            self._output_transcript_buffer, False
                        )

                if server_content.model_turn:
                    for part in server_content.model_turn.parts:
                        if part.inline_data and part.inline_data.data:
                            if not self._tts_started_for_turn:
                                self._tts_started_for_turn = True
                                self._model_speaking = True
                                if self._on_tts_start:
                                    await self._on_tts_start()
                            if self._on_audio_chunk:
                                await self._on_audio_chunk(part.inline_data.data)

                turn_complete = getattr(server_content, "turn_complete", False)
                interrupted = getattr(server_content, "interrupted", False)

                if turn_complete or interrupted:
                    if turn_complete:
                        logger.info("turn_complete received")
                    if interrupted:
                        logger.info("interrupted received")

                    if self._input_transcript_buffer and self._on_input_transcript:
                        await self._on_input_transcript(self._input_transcript_buffer, True)
                    if self._output_transcript_buffer and self._on_transcript:
                        await self._on_transcript(self._output_transcript_buffer, True)
                    if self._input_transcript_buffer.strip():
                        self._history.append({"role": "user", "text": self._input_transcript_buffer.strip()})
                    if self._output_transcript_buffer.strip():
                        self._history.append({"role": "model", "text": self._output_transcript_buffer.strip()})
                    if len(self._history) > self._max_history_turns * 2:
                        self._history = self._history[-self._max_history_turns * 2 :]
                    self._input_transcript_buffer = ""
                    self._output_transcript_buffer = ""

                    if self._tts_started_for_turn:
                        self._tts_started_for_turn = False
                        self._model_speaking = False
                        if self._on_tts_end:
                            await self._on_tts_end()

                    if self._state != "listening":
                        self._state = "listening"
                        if self._on_state:
                            await self._on_state("listening")

                    # Workaround for broken multi-turn: reopen the Gemini
                    # session using session_resumption so the next user audio
                    # triggers a fresh VAD cycle with preserved context.
                    asyncio.create_task(self._reopen_session())
                    return

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("Gemini Live receive error: %s", exc)

    async def _handle_tool_call(self, function_calls: list[Any]) -> None:
        """Execute tool_call requests emitted by Gemini Live and send back responses."""
        from google.genai import types

        responses: list[types.FunctionResponse] = []
        for fc in function_calls:
            fname = getattr(fc, "name", None)
            fid = getattr(fc, "id", None)
            fargs = dict(getattr(fc, "args", {}) or {})
            fn = self._tool_funcs.get(fname)
            logger.info("[gemini] tool_call name=%s id=%s args_keys=%s",
                         fname, fid, list(fargs.keys()))
            if fn is None:
                result: dict[str, Any] = {"error": f"Unknown tool: {fname}"}
            else:
                try:
                    if inspect.iscoroutinefunction(fn):
                        raw = await fn(**fargs)
                    else:
                        raw = await asyncio.to_thread(fn, **fargs)
                    result = raw if isinstance(raw, dict) else {"result": raw}
                except TypeError as exc:
                    result = {"error": f"Invalid arguments for {fname}: {exc}"}
                except Exception as exc:
                    logger.exception("[gemini] tool %s failed", fname)
                    result = {"error": f"Tool {fname} failed: {exc}"}
            responses.append(
                types.FunctionResponse(id=fid, name=fname, response=result)
            )
        if responses and self._session is not None:
            try:
                await self._session.send_tool_response(function_responses=responses)
            except Exception as exc:
                logger.warning("[gemini] send_tool_response failed: %s", exc)

    async def send_audio(self, chunk: bytes) -> None:
        if not self._running or self._session is None:
            return
        # Drop audio during the reopen window — the Gemini WS is mid-handshake
        # and send_realtime_input would raise, killing the frontend WS.
        if self._reopening:
            return
        if self._model_speaking:
            return
        from google.genai import types

        try:
            await self._session.send_realtime_input(
                audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000")
            )
        except Exception as exc:
            logger.warning("[gemini] send_audio dropped (%s) — likely mid-reopen", exc)

    async def send_text(self, text: str) -> None:
        if not self._running or self._session is None:
            return
        await self._session.send_client_content(
            turns=[{"role": "user", "parts": [{"text": text}]}],
            turn_complete=True,
        )

    async def stop(self) -> None:
        self._running = False
        if self._receive_task is not None:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
            self._receive_task = None

        if self._session is not None:
            try:
                if getattr(self, "_session_cm", None) is not None:
                    await self._session_cm.__aexit__(None, None, None)
                else:
                    await self._session.close()
            except Exception as exc:
                logger.warning("Error closing Gemini Live session: %s", exc)
            self._session = None
            self._session_cm = None
        logger.info("Gemini Live session stopped")


# ---------------------------------------------------------------------------
# Fallback providers (used when GEMINI_API_KEY is not available)
# ---------------------------------------------------------------------------

DEEPGRAM_WS_URL = "wss://api.deepgram.com/v1/listen"
OPENAI_TTS_URL = "https://api.openai.com/v1/audio/speech"
ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech"
OPENAI_TTS_VOICES = {"alloy", "echo", "fable", "onyx", "nova", "shimmer"}
ELEVENLABS_VOICE_MAP: dict[str, str] = {
    "alloy": "21m00Tcm4TlvDq8ikWAM",
    "echo": "29vD33N1CtxCmqQRPOHJ",
    "fable": "EXAVITQu4vr4xnSDxMaL",
    "onyx": "TxGEqnHWrfWFTfGW9XjX",
    "nova": "pNInz6obpgDQGcFmaJgB",
    "shimmer": "MF3mGyEYCl7XYWbV9V6O",
}
TTS_CHUNK_SIZE = 4096


class FallbackSTT:
    """Real-time STT via Deepgram WebSocket (fallback when Gemini unavailable)."""

    def __init__(self) -> None:
        self._ws = None
        self._running: bool = False
        self._callback: TranscriptCallback | None = None
        self._listen_task: asyncio.Task | None = None

    async def start(
        self,
        language: str = "auto",
        on_transcript_callback: TranscriptCallback | None = None,
    ) -> None:
        import websockets

        self._callback = on_transcript_callback
        self._running = True

        deepgram_key = os.environ.get("DEEPGRAM_API_KEY")
        if not deepgram_key:
            raise EnvironmentError("DEEPGRAM_API_KEY not set")

        params = (
            "model=nova-2&smart_format=true"
            "&interim_results=true&utterance_end_ms=1000&vad_events=true"
        )
        if language and language != "auto":
            params += f"&language={language}"

        url = f"{DEEPGRAM_WS_URL}?{params}"
        self._ws = await websockets.connect(
            url, additional_headers={"Authorization": f"Token {deepgram_key}"}
        )
        self._listen_task = asyncio.create_task(self._listen_loop())

    async def _listen_loop(self) -> None:
        import websockets

        if self._ws is None:
            return
        try:
            async for raw_message in self._ws:
                if not self._running:
                    break
                if isinstance(raw_message, bytes):
                    continue
                try:
                    message = json.loads(raw_message)
                except json.JSONDecodeError:
                    continue
                if message.get("type") == "Results":
                    channel = message.get("channel", {})
                    alternatives = channel.get("alternatives", [])
                    if alternatives:
                        transcript = alternatives[0].get("transcript", "")
                        if transcript and self._callback:
                            is_final = message.get("is_final", False)
                            await self._callback(transcript, is_final)
        except websockets.ConnectionClosed:
            logger.info("Deepgram WebSocket closed")
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("Deepgram listener error: %s", exc)

    async def send_audio(self, chunk: bytes) -> None:
        if self._running and self._ws is not None:
            await self._ws.send(chunk)

    async def stop(self) -> None:
        self._running = False
        if self._listen_task is not None:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
            self._listen_task = None
        if self._ws is not None:
            try:
                await self._ws.send(json.dumps({"type": "CloseStream"}))
                await self._ws.close()
            except Exception as exc:
                logger.warning("Error closing Deepgram WS: %s", exc)
            self._ws = None


class FallbackTTS:
    """Streaming TTS via OpenAI/ElevenLabs (fallback when Gemini unavailable)."""

    _PROVIDER_ORDER = ["openai", "elevenlabs"]

    async def stream_tts(
        self,
        text: str,
        voice: str = "alloy",
        provider: str = "auto",
        on_chunk_callback: AudioChunkCallback | None = None,
    ) -> None:
        providers = self._PROVIDER_ORDER if provider == "auto" else [provider]
        errors: list[str] = []

        for prov in providers:
            env_key = {"openai": "OPENAI_API_KEY", "elevenlabs": "ELEVENLABS_API_KEY"}.get(prov, "")
            if not os.environ.get(env_key):
                errors.append(f"{prov}: {env_key} not set")
                continue
            try:
                if prov == "openai":
                    await self._stream_openai(text, voice, on_chunk_callback)
                elif prov == "elevenlabs":
                    await self._stream_elevenlabs(text, voice, on_chunk_callback)
                return
            except Exception as exc:
                logger.warning("TTS provider %s failed: %s", prov, exc)
                errors.append(f"{prov}: {exc}")

        raise RuntimeError("No TTS provider available: " + "; ".join(errors))

    async def _stream_openai(self, text: str, voice: str, cb: AudioChunkCallback | None) -> None:
        api_key = os.environ["OPENAI_API_KEY"]
        if voice not in OPENAI_TTS_VOICES:
            voice = "alloy"
        async with httpx.AsyncClient() as client:
            async with client.stream(
                "POST", OPENAI_TTS_URL,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": "tts-1", "input": text, "voice": voice, "response_format": "mp3"},
                timeout=120,
            ) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes(TTS_CHUNK_SIZE):
                    if cb:
                        await cb(chunk)

    async def _stream_elevenlabs(self, text: str, voice: str, cb: AudioChunkCallback | None) -> None:
        api_key = os.environ["ELEVENLABS_API_KEY"]
        voice_id = ELEVENLABS_VOICE_MAP.get(voice, "21m00Tcm4TlvDq8ikWAM")
        async with httpx.AsyncClient() as client:
            async with client.stream(
                "POST", f"{ELEVENLABS_TTS_URL}/{voice_id}/stream",
                headers={"xi-api-key": api_key, "Content-Type": "application/json", "Accept": "audio/mpeg"},
                json={"text": text, "model_id": "eleven_multilingual_v2", "output_format": "mp3_44100_128"},
                timeout=120,
            ) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes(TTS_CHUNK_SIZE):
                    if cb:
                        await cb(chunk)

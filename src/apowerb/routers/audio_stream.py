"""WebSocket endpoint for real-time audio streaming.

Primary: Gemini Live API (unified STT+TTS in one session).
Fallback: Deepgram STT + OpenAI/ElevenLabs TTS (separate services).
"""

import asyncio
import json
import os
from logging import getLogger
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from jose import JWTError, jwt

_ENV_PATH = Path(__file__).resolve().parents[3] / ".env"
if _ENV_PATH.exists():
    load_dotenv(_ENV_PATH, override=False)

from apowerb.configs.settings import get_settings
from apowerb.configs.paths import uploads_dir
from apowerb.helpers.security import get_algorithm, get_secret_key
from apowerb.core.audio_streaming import (
    FallbackSTT,
    FallbackTTS,
    GeminiLiveSession,
)

logger = getLogger(__name__)
router = APIRouter()
settings = get_settings()


async def _validate_ws_token(token: str) -> str | None:
    # Hors du try : un jeton invalide vaut None, une clé absente vaut une erreur.
    secret = get_secret_key()
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[get_algorithm()],
        )
        return payload.get("sub")
    except JWTError as exc:
        logger.warning("WebSocket JWT validation failed: %s", exc)
        return None


def _use_gemini() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY"))


def _load_knowledge_context(agent_name: str, session_id: str | None) -> str | None:
    """Return a human-readable list of indexed documents for the current scope.

    Reads the .knowledge_map.json written by /api/rag/* endpoints, trying the
    session scope first and falling back to the agent scope — same resolution
    order as the chat-text before_model callback.
    """
    candidates: list[str] = []
    if session_id:
        candidates.append(str(uploads_dir() / session_id / ".knowledge_map.json"))
    candidates.append(str(uploads_dir() / agent_name / ".knowledge_map.json"))

    for path in candidates:
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r") as f:
                kmap = json.load(f)
        except Exception as exc:
            logger.warning("[audio_ws] failed to read %s: %s", path, exc)
            continue

        indexed = [
            s for s in kmap.get("sources", []) if s.get("status") == "complete"
        ]
        failed = [s for s in kmap.get("sources", []) if s.get("status") == "error"]
        pending = [
            s for s in kmap.get("sources", [])
            if s.get("status") not in ("complete", "error")
        ]

        if not (indexed or failed or pending):
            return None

        lines = ["[RAG CONTEXT] Documents available in this session:"]
        for s in indexed:
            lines.append(
                f"  - '{s.get('name')}' (knowledge_id={s.get('knowledge_id')}) — indexed"
            )
        for s in pending:
            lines.append(f"  - '{s.get('name')}' — still indexing")
        for s in failed:
            lines.append(f"  - '{s.get('name')}' — indexation failed")
        lines.append(
            "When the user asks what has been indexed, answer with the list above. "
            "When they ask a question about the content, call tool_search_knowledge "
            "with the matching knowledge_id and answer from the returned excerpts. "
            "Never invent information that is not in the search result."
        )
        return "\n".join(lines)

    return None


# Tools that make no sense in Gemini Live voice mode: the model already does
# STT+TTS natively, and file-path STT/TTS callables would be invoked with a
# hallucinated path. Skip them silently when exposing tools to Gemini Live.
_VOICE_INCOMPATIBLE_TOOLS = frozenset({
    "tool_speech_to_text",
    "tool_text_to_speech",
    "tool_transcribe_audio_file",
})


def _resolve_agent_tools(agent_name: str | None) -> list:
    """Return the list of Python callables representing the agent's native tools.

    Combines tools declared on the agent (agent_tools DB field, tool_config refs)
    with the tools bundled by the SuperAgent template, when applicable.
    Filters out tools incompatible with the voice streaming session.
    """
    if not agent_name or not agent_name.startswith("agent"):
        return []
    try:
        from apowerb.core.agent_helpers import get_agent_details
        from apowerb.tools_store.tools_helpers import load_agent_tools_functions

        agent_id = int(agent_name.replace("agent", ""))
        details = get_agent_details(agent_id=agent_id)
        if not details:
            return []

        raw_tools = details.get("agent_tools") or "[]"
        try:
            tools_ids = json.loads(raw_tools) if isinstance(raw_tools, str) else list(raw_tools)
        except Exception:
            tools_ids = []

        template_id = details.get("superagent_template_id")
        if template_id:
            from apowerb.core.superagents import SUPERAGENT_TEMPLATES
            for tpl in SUPERAGENT_TEMPLATES:
                if tpl.get("template_id") == template_id:
                    for name in tpl.get("recommended_tools") or []:
                        if name not in tools_ids:
                            tools_ids.append(name)
                    break

        agent_owner = details.get("owner_id") or ""
        os.environ.setdefault("AGENT_OWNER", agent_owner)
        os.environ.setdefault("ROOT_AGENT_ID", agent_name)
        os.environ.setdefault("AGENT_ID", agent_name)

        _names, funcs = load_agent_tools_functions(
            tools=tools_ids, owner_id=agent_owner
        )
        resolved = []
        skipped = []
        for fn in funcs:
            if not callable(fn):
                continue
            fname = getattr(fn, "__name__", "")
            if fname in _VOICE_INCOMPATIBLE_TOOLS:
                skipped.append(fname)
                continue
            resolved.append(fn)
        if skipped:
            logger.info("[audio_ws] skipped voice-incompatible tools: %s", skipped)
        return resolved
    except Exception as exc:
        logger.warning("[audio_ws] failed to resolve agent tools: %s", exc)
        return []


def _build_agent_system_instruction(
    agent_name: str | None,
    agent_display_name: str | None,
    session_id: str | None = None,
) -> str | None:
    """Look up the chat agent's persona and turn it into a Gemini Live system prompt."""
    if not agent_name or not isinstance(agent_name, str):
        return None
    try:
        from apowerb.core.agent_helpers import get_agent_details
        if not agent_name.startswith("agent"):
            return None
        agent_id = int(agent_name.replace("agent", ""))
        details = get_agent_details(agent_id=agent_id)
        if not details:
            return None
        instruction = (details.get("agent_instruction") or "").strip()
        description = (details.get("agent_description") or "").strip()
        display = agent_display_name or details.get("agent_name") or agent_name
        parts: list[str] = []
        parts.append(
            f"You are '{display}', a voice assistant. Reply in the same language as the user. "
            "Keep answers conversational and concise — this is a spoken interaction."
        )
        if description:
            parts.append(f"Role description:\n{description}")
        if instruction:
            parts.append(f"Detailed instructions:\n{instruction}")
        rag_context = _load_knowledge_context(agent_name, session_id)
        if rag_context:
            parts.append(rag_context)
        return "\n\n".join(parts)
    except Exception as exc:
        logger.warning("[audio_ws] failed to build agent system instruction: %s", exc)
        return None


_VALID_MODES = {"conversation", "dictation"}


@router.post("/audio/transcribe")
async def transcribe_audio(file: UploadFile = File(...)) -> dict:
    """One-shot transcription of an uploaded audio blob via Gemini.

    Used by the chat textarea mic button (dictation). The frontend records
    a chunk via MediaRecorder and POSTs it here; we return plain text.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise HTTPException(status_code=503, detail="Gemini API key not configured")

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty audio payload")
    if len(raw) > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Audio too large (max 25MB)")

    mime_type = file.content_type or "audio/webm"

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)
        response = await asyncio.to_thread(
            client.models.generate_content,
            model="gemini-2.5-flash",
            contents=[
                types.Part.from_bytes(data=raw, mime_type=mime_type),
                "Transcribe this audio verbatim in the same language as spoken. "
                "Return only the transcript text — no preamble, no quotes, no commentary.",
            ],
        )
        text = (getattr(response, "text", None) or "").strip()
        return {"text": text}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("[audio_transcribe] failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Transcription failed: {exc}") from exc


@router.websocket("/audio/ws/{session_id}")
async def audio_websocket(websocket: WebSocket, session_id: str) -> None:
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=4001, reason="Missing token")
        return

    try:
        email = await _validate_ws_token(token)
    except RuntimeError as exc:
        # Clé de signature absente : le serveur est mal configuré, l'appelant
        # n'y est pour rien. Starlette ne convertit pas les exceptions en
        # réponse propre sur un scope WebSocket (ServerErrorMiddleware ignore
        # tout scope != "http") : sans ce garde, la connexion s'effondrerait
        # avant le moindre accept/close, sans code de fermeture.
        logger.error("Audio WS refusé — configuration serveur : %s", exc)
        await websocket.close(code=4500, reason="Server misconfigured")
        return
    if not email:
        await websocket.close(code=4003, reason="Invalid token")
        return

    mode = websocket.query_params.get("mode", "conversation")
    if mode not in _VALID_MODES:
        mode = "conversation"

    await websocket.accept()
    logger.info(
        "Audio WS connected: session=%s user=%s mode=%s", session_id, email, mode
    )

    if mode == "dictation":
        try:
            await websocket.send_json({
                "type": "error",
                "message": (
                    "Dictation mode is handled client-side via Web Speech API; "
                    "do not connect this WS"
                ),
            })
        except Exception as exc:
            logger.warning("Failed to send dictation-mode error: %s", exc)
        finally:
            try:
                await websocket.close(code=1000, reason="dictation-mode-not-supported")
            except Exception:
                pass
        return

    if _use_gemini():
        await _handle_gemini_mode(websocket, session_id)
    else:
        await _handle_fallback_mode(websocket, session_id)


async def _handle_gemini_mode(websocket: WebSocket, session_id: str) -> None:
    """Gemini Live: single session handles both STT and TTS."""
    session: GeminiLiveSession | None = None

    async def on_audio_chunk(chunk: bytes) -> None:
        try:
            await websocket.send_bytes(chunk)
        except Exception:
            pass

    async def on_tts_start() -> None:
        try:
            await websocket.send_json({"type": "tts_start"})
        except Exception:
            pass

    async def on_tts_end() -> None:
        try:
            await websocket.send_json({"type": "tts_end"})
        except Exception:
            pass

    async def on_output_transcript(text: str, is_final: bool) -> None:
        try:
            await websocket.send_json({
                "type": "transcript",
                "text": text,
                "is_final": is_final,
                "direction": "output",
            })
        except Exception:
            pass

    async def on_input_transcript(text: str, is_final: bool) -> None:
        try:
            await websocket.send_json({
                "type": "transcript",
                "text": text,
                "is_final": is_final,
                "direction": "input",
            })
        except Exception:
            pass

    async def on_state(state: str) -> None:
        try:
            await websocket.send_json({"type": "state", "state": state})
        except Exception:
            pass

    try:
        while True:
            raw = await websocket.receive()

            if raw.get("type") == "websocket.disconnect":
                break

            if "text" in raw:
                try:
                    message: dict[str, Any] = json.loads(raw["text"])
                except json.JSONDecodeError:
                    await websocket.send_json({"type": "error", "message": "Invalid JSON"})
                    continue

                msg_type = message.get("type", "")

                if msg_type == "ping":
                    await websocket.send_json({"type": "pong"})

                elif msg_type == "start_listening":
                    if session is None:
                        voice = message.get("voice", "Kore")
                        language = message.get("language", "auto")
                        agent_name = message.get("agent_name")
                        agent_display_name = message.get("agent_display_name")
                        system_instruction = _build_agent_system_instruction(
                            agent_name, agent_display_name, session_id
                        )
                        agent_tools = _resolve_agent_tools(agent_name)
                        session = GeminiLiveSession()
                        await session.start(
                            voice=voice,
                            language=language,
                            system_instruction=system_instruction,
                            on_transcript=on_output_transcript,
                            on_audio_chunk=on_audio_chunk,
                            on_input_transcript=on_input_transcript,
                            on_state=on_state,
                            on_tts_start=on_tts_start,
                            on_tts_end=on_tts_end,
                            tools=agent_tools,
                        )
                    await websocket.send_json({"type": "listening_started", "mode": "gemini_live"})

                elif msg_type == "stop_listening":
                    await websocket.send_json({"type": "listening_stopped"})

                elif msg_type == "start_speaking":
                    text = message.get("text", "")
                    if not text:
                        await websocket.send_json({"type": "error", "message": "Missing text for TTS"})
                        continue
                    if session is None:
                        voice = message.get("voice", "Kore")
                        session = GeminiLiveSession()
                        await session.start(
                            voice=voice,
                            on_transcript=on_output_transcript,
                            on_audio_chunk=on_audio_chunk,
                            on_input_transcript=on_input_transcript,
                            on_state=on_state,
                            on_tts_start=on_tts_start,
                            on_tts_end=on_tts_end,
                        )
                    await session.send_text(text)

                elif msg_type == "stop_speaking":
                    await websocket.send_json({"type": "tts_end"})

                else:
                    await websocket.send_json({"type": "error", "message": f"Unknown type: {msg_type}"})

            elif "bytes" in raw:
                if session is not None:
                    _audio_frame_counter = getattr(websocket.state, "_audio_frames", 0) + 1
                    websocket.state._audio_frames = _audio_frame_counter
                    if _audio_frame_counter % 50 == 1:
                        logger.info("[audio_ws] rx frame #%d bytes=%d", _audio_frame_counter, len(raw["bytes"]))
                    try:
                        await session.send_audio(raw["bytes"])
                    except Exception as exc:
                        logger.warning("[audio_ws] send_audio failed (frame #%d): %s — keeping client WS open", _audio_frame_counter, exc)

    except WebSocketDisconnect:
        logger.info("Audio WS disconnected: session=%s", session_id)
    except Exception as exc:
        logger.error("Audio WS error: session=%s error=%s", session_id, exc)
    finally:
        if session is not None:
            await session.stop()


async def _handle_fallback_mode(websocket: WebSocket, session_id: str) -> None:
    """Fallback: Deepgram STT + OpenAI/ElevenLabs TTS (separate services)."""
    stt: FallbackSTT | None = None
    tts_task: asyncio.Task[None] | None = None

    async def on_transcript(text: str, is_final: bool) -> None:
        try:
            await websocket.send_json({
                "type": "transcript",
                "text": text,
                "is_final": is_final,
                "direction": "input",
            })
        except Exception:
            pass

    async def on_tts_chunk(chunk: bytes) -> None:
        try:
            await websocket.send_json({"type": "audio_chunk", "format": "mp3"})
            await websocket.send_bytes(chunk)
        except Exception:
            pass

    try:
        while True:
            raw = await websocket.receive()

            if raw.get("type") == "websocket.disconnect":
                break

            if "text" in raw:
                try:
                    message: dict[str, Any] = json.loads(raw["text"])
                except json.JSONDecodeError:
                    await websocket.send_json({"type": "error", "message": "Invalid JSON"})
                    continue

                msg_type = message.get("type", "")

                if msg_type == "ping":
                    await websocket.send_json({"type": "pong"})

                elif msg_type == "start_listening":
                    language = message.get("language", "auto")
                    stt = FallbackSTT()
                    await stt.start(language=language, on_transcript_callback=on_transcript)
                    await websocket.send_json({"type": "listening_started", "mode": "fallback"})

                elif msg_type == "stop_listening":
                    if stt is not None:
                        await stt.stop()
                        stt = None
                    await websocket.send_json({"type": "listening_stopped"})

                elif msg_type == "start_speaking":
                    text = message.get("text", "")
                    voice = message.get("voice", "alloy")
                    provider = message.get("provider", "auto")
                    if not text:
                        await websocket.send_json({"type": "error", "message": "Missing text"})
                        continue

                    await websocket.send_json({"type": "tts_start"})

                    async def _run_tts(t: str, v: str, p: str) -> None:
                        try:
                            tts = FallbackTTS()
                            await tts.stream_tts(text=t, voice=v, provider=p, on_chunk_callback=on_tts_chunk)
                        except Exception as exc:
                            logger.error("Fallback TTS error: %s", exc)
                            try:
                                await websocket.send_json({"type": "error", "message": f"TTS error: {exc}"})
                            except Exception:
                                pass
                        finally:
                            try:
                                await websocket.send_json({"type": "tts_end"})
                            except Exception:
                                pass

                    tts_task = asyncio.create_task(_run_tts(text, voice, provider))

                elif msg_type == "stop_speaking":
                    if tts_task is not None:
                        tts_task.cancel()
                        try:
                            await tts_task
                        except asyncio.CancelledError:
                            pass
                        tts_task = None
                    await websocket.send_json({"type": "tts_end"})

                else:
                    await websocket.send_json({"type": "error", "message": f"Unknown type: {msg_type}"})

            elif "bytes" in raw:
                if stt is not None:
                    await stt.send_audio(raw["bytes"])

    except WebSocketDisconnect:
        logger.info("Audio WS disconnected (fallback): session=%s", session_id)
    except Exception as exc:
        logger.error("Audio WS error (fallback): session=%s error=%s", session_id, exc)
    finally:
        if stt is not None:
            await stt.stop()
        if tts_task is not None:
            tts_task.cancel()
            try:
                await tts_task
            except asyncio.CancelledError:
                pass

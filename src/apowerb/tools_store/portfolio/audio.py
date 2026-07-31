"""Audio tools — multi-provider support for speech-to-text, text-to-speech, and analysis.

Providers for STT (in priority order):
1. OpenAI Whisper — uses OPENAI_API_KEY
2. Deepgram Nova-2 — uses DEEPGRAM_API_KEY
3. Gemini — uses GEMINI_API_KEY

Providers for TTS (in priority order):
1. OpenAI TTS — uses OPENAI_API_KEY
2. ElevenLabs — uses ELEVENLABS_API_KEY

The tools save generated audio to the agent's uploads folder and return
metadata so the UI can play or download the audio.
"""

import base64
import os
import re
import time
from logging import getLogger
from pathlib import Path

from apowerb.configs.paths import agent_upload_dir

logger = getLogger(__name__)

# API keys used by this module — declared at module level so the ToolsStore
# parameter scanner (regex on os.getenv) can discover them for the UI.
_OPENAI_KEY = os.getenv("OPENAI_API_KEY", "")
_DEEPGRAM_KEY = os.getenv("DEEPGRAM_API_KEY", "")
_GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
_ELEVENLABS_KEY = os.getenv("ELEVENLABS_API_KEY", "")

# MIME type mapping for audio formats
_MIME_MAP = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
    ".webm": "audio/webm",
    ".flac": "audio/flac",
    ".aac": "audio/aac",
    ".wma": "audio/x-ms-wma",
    ".opus": "audio/opus",
}


def _sanitize_filename(text: str, max_len: int = 40) -> str:
    """Turn a text into a safe filename slug."""
    slug = re.sub(r"[^\w\s-]", "", text.lower().strip())
    slug = re.sub(r"[\s_-]+", "_", slug)
    return slug[:max_len] or "generated"


# ---------------------------------------------------------------------------
# STT Provider: OpenAI Whisper
# ---------------------------------------------------------------------------

def _stt_openai(audio_path: str, language: str) -> dict:
    """Transcribe audio via OpenAI Whisper API.

    Returns dict with transcription, language_detected, duration_seconds.
    """
    import httpx

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise EnvironmentError("OPENAI_API_KEY not set")

    ext = Path(audio_path).suffix.lower()
    mime = _MIME_MAP.get(ext, "audio/mpeg")
    filename = Path(audio_path).name

    with open(audio_path, "rb") as f:
        audio_data = f.read()

    form_data = {
        "model": (None, "whisper-1"),
    }
    if language and language != "auto":
        form_data["language"] = (None, language)

    files = {
        "file": (filename, audio_data, mime),
    }

    resp = httpx.post(
        "https://api.openai.com/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {api_key}"},
        data={k: v[1] for k, v in form_data.items()},
        files=files,
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()

    transcription = data.get("text", "")
    detected_lang = data.get("language", language if language != "auto" else "unknown")
    duration = data.get("duration", None)

    return {
        "transcription": transcription,
        "language_detected": detected_lang,
        "duration_seconds": duration,
    }


# ---------------------------------------------------------------------------
# STT Provider: Deepgram
# ---------------------------------------------------------------------------

def _stt_deepgram(audio_path: str, language: str) -> dict:
    """Transcribe audio via Deepgram Nova-2 API.

    Returns dict with transcription, language_detected, duration_seconds.
    """
    import httpx

    api_key = os.environ.get("DEEPGRAM_API_KEY")
    if not api_key:
        raise EnvironmentError("DEEPGRAM_API_KEY not set")

    ext = Path(audio_path).suffix.lower()
    mime = _MIME_MAP.get(ext, "audio/mpeg")

    with open(audio_path, "rb") as f:
        audio_data = f.read()

    params = "model=nova-2&smart_format=true"
    if language and language != "auto":
        params += f"&language={language}"
    else:
        params += "&detect_language=true"

    resp = httpx.post(
        f"https://api.deepgram.com/v1/listen?{params}",
        headers={
            "Authorization": f"Token {api_key}",
            "Content-Type": mime,
        },
        content=audio_data,
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()

    results = data.get("results", {})
    channels = results.get("channels", [{}])
    alternatives = channels[0].get("alternatives", [{}]) if channels else [{}]
    transcription = alternatives[0].get("transcript", "") if alternatives else ""

    detected_lang = (
        results.get("channels", [{}])[0]
        .get("detected_language", language if language != "auto" else "unknown")
        if channels
        else "unknown"
    )

    metadata = data.get("metadata", {})
    duration = metadata.get("duration", None)

    return {
        "transcription": transcription,
        "language_detected": detected_lang,
        "duration_seconds": duration,
    }


# ---------------------------------------------------------------------------
# STT Provider: Gemini
# ---------------------------------------------------------------------------

def _stt_gemini(audio_path: str, language: str) -> dict:
    """Transcribe audio via Gemini (upload + generate_content).

    Returns dict with transcription, language_detected, duration_seconds.
    """
    from google import genai

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise EnvironmentError("GEMINI_API_KEY not set")

    client = genai.Client(api_key=api_key)

    ext = Path(audio_path).suffix.lower()
    mime = _MIME_MAP.get(ext, "audio/mpeg")

    uploaded_file = client.files.upload(
        file=audio_path,
        config={"mime_type": mime},
    )

    lang_hint = f" The audio is in {language}." if language and language != "auto" else ""
    prompt = (
        f"Transcribe this audio file accurately and completely.{lang_hint} "
        "Return ONLY the transcription text, nothing else. "
        "If you detect the language, mention it at the very end in the format: [Language: XX]"
    )

    response = client.models.generate_content(
        model="gemini-2.0-flash",
        contents=[uploaded_file, prompt],
    )

    text = response.text.strip() if response.text else ""

    # Try to extract language tag from end of response
    detected_lang = "unknown"
    lang_match = re.search(r"\[Language:\s*(\w+)\]", text)
    if lang_match:
        detected_lang = lang_match.group(1)
        text = text[: lang_match.start()].strip()

    return {
        "transcription": text,
        "language_detected": detected_lang,
        "duration_seconds": None,
    }


# ---------------------------------------------------------------------------
# STT Provider registry
# ---------------------------------------------------------------------------

_STT_PROVIDERS = {
    "openai": ("OpenAI Whisper", _stt_openai, "OPENAI_API_KEY"),
    "deepgram": ("Deepgram Nova-2", _stt_deepgram, "DEEPGRAM_API_KEY"),
    "gemini": ("Gemini", _stt_gemini, "GEMINI_API_KEY"),
}

_STT_PROVIDER_ORDER = ["openai", "deepgram", "gemini"]


# ---------------------------------------------------------------------------
# TTS Provider: OpenAI
# ---------------------------------------------------------------------------

_OPENAI_TTS_VOICES = {"alloy", "echo", "fable", "onyx", "nova", "shimmer"}


def _tts_openai(text: str, voice: str, output_format: str) -> tuple[bytes, str]:
    """Generate speech via OpenAI TTS API.

    Returns (audio_bytes, content_type).
    """
    import httpx

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise EnvironmentError("OPENAI_API_KEY not set")

    # Validate voice
    if voice not in _OPENAI_TTS_VOICES:
        voice = "alloy"

    resp = httpx.post(
        "https://api.openai.com/v1/audio/speech",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": "tts-1",
            "input": text,
            "voice": voice,
            "response_format": output_format,
        },
        timeout=120,
    )
    resp.raise_for_status()

    content_type = _MIME_MAP.get(f".{output_format}", "audio/mpeg")
    return resp.content, content_type


# ---------------------------------------------------------------------------
# TTS Provider: ElevenLabs
# ---------------------------------------------------------------------------

_ELEVENLABS_VOICE_MAP = {
    "alloy": "21m00Tcm4TlvDq8ikWAM",
    "echo": "29vD33N1CtxCmqQRPOHJ",
    "fable": "EXAVITQu4vr4xnSDxMaL",
    "onyx": "TxGEqnHWrfWFTfGW9XjX",
    "nova": "pNInz6obpgDQGcFmaJgB",
    "shimmer": "MF3mGyEYCl7XYWbV9V6O",
}

_ELEVENLABS_DEFAULT_VOICE = "21m00Tcm4TlvDq8ikWAM"


def _tts_elevenlabs(text: str, voice: str, output_format: str) -> tuple[bytes, str]:
    """Generate speech via ElevenLabs API.

    Returns (audio_bytes, content_type).
    """
    import httpx

    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        raise EnvironmentError("ELEVENLABS_API_KEY not set")

    voice_id = _ELEVENLABS_VOICE_MAP.get(voice, _ELEVENLABS_DEFAULT_VOICE)

    # ElevenLabs output format mapping
    _el_format_map = {
        "mp3": ("mp3_44100_128", "audio/mpeg"),
        "wav": ("pcm_44100", "audio/wav"),
        "ogg": ("mp3_44100_128", "audio/mpeg"),  # fallback to mp3
        "opus": ("mp3_44100_128", "audio/mpeg"),
        "aac": ("mp3_44100_128", "audio/mpeg"),
        "flac": ("pcm_44100", "audio/wav"),  # fallback to pcm
    }
    el_fmt, actual_content_type = _el_format_map.get(output_format, ("mp3_44100_128", "audio/mpeg"))

    resp = httpx.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
        headers={
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": actual_content_type,
        },
        json={
            "text": text,
            "model_id": "eleven_multilingual_v2",
            "output_format": el_fmt,
        },
        timeout=120,
    )
    resp.raise_for_status()

    return resp.content, actual_content_type


# ---------------------------------------------------------------------------
# TTS Provider registry
# ---------------------------------------------------------------------------

_TTS_PROVIDERS = {
    "openai": ("OpenAI TTS", _tts_openai, "OPENAI_API_KEY"),
    "elevenlabs": ("ElevenLabs", _tts_elevenlabs, "ELEVENLABS_API_KEY"),
}

_TTS_PROVIDER_ORDER = ["openai", "elevenlabs"]


# ---------------------------------------------------------------------------
# Audio generation provider registry (stub — for future providers)
# ---------------------------------------------------------------------------

_AUDIOGEN_PROVIDERS: dict = {}

_AUDIOGEN_PROVIDER_ORDER: list = []


# ---------------------------------------------------------------------------
# Main tools
# ---------------------------------------------------------------------------

def tool_speech_to_text(
    audio_file_path: str,
    language: str = "auto",
    provider: str = "auto",
) -> dict:
    """Transcribe an audio file to text using speech-to-text.

    Processes an audio file and returns the transcription using available
    STT providers in priority order.

    Args:
        audio_file_path (str): Path to the audio file to transcribe. Supported
                               formats: mp3, wav, ogg, m4a, webm, flac, aac, wma, opus.
        language (str): Language of the audio. Use "auto" for automatic detection,
                        or provide an ISO 639-1 code (e.g., "en", "fr", "es").
                        Default: "auto".
        provider (str): Which STT provider to use. Options: "auto" (tries available
                        providers in order), "openai", "deepgram", "gemini".
                        Default: "auto".

    Returns:
        dict: Contains status, transcription, language_detected, duration_seconds,
              provider_used, word_count, or error_message if transcription fails.
    """
    # Validate file exists
    if not os.path.isfile(audio_file_path):
        return {
            "status": "error",
            "error_message": f"Audio file not found: {audio_file_path}",
        }

    # Validate extension
    ext = Path(audio_file_path).suffix.lower()
    if ext not in _MIME_MAP:
        return {
            "status": "error",
            "error_message": (
                f"Unsupported audio format '{ext}'. "
                f"Supported: {', '.join(sorted(_MIME_MAP.keys()))}"
            ),
        }

    # Determine provider order
    if provider == "auto":
        order = _STT_PROVIDER_ORDER
    elif provider in _STT_PROVIDERS:
        order = [provider]
    else:
        return {
            "status": "error",
            "error_message": (
                f"Unknown provider '{provider}'. "
                f"Valid: auto, {', '.join(_STT_PROVIDERS.keys())}"
            ),
        }

    # Try each provider
    errors = []
    for prov_key in order:
        prov_name, stt_fn, env_key = _STT_PROVIDERS[prov_key]

        if not os.environ.get(env_key):
            errors.append(f"{prov_name}: {env_key} not set")
            continue

        try:
            logger.info("[STT] Transcribing with %s: %s", prov_name, audio_file_path)
            result = stt_fn(audio_file_path, language)

            transcription = result.get("transcription", "")
            word_count = len(transcription.split()) if transcription else 0

            logger.info(
                "[STT] Transcribed %d words via %s",
                word_count,
                prov_name,
            )

            return {
                "status": "success",
                "transcription": transcription,
                "language_detected": result.get("language_detected", "unknown"),
                "duration_seconds": result.get("duration_seconds"),
                "provider_used": prov_name,
                "word_count": word_count,
            }

        except Exception as e:
            logger.warning("[STT] %s failed: %s", prov_name, e)
            errors.append(f"{prov_name}: {e}")
            continue

    # All providers failed
    return {
        "status": "error",
        "error_message": (
            "Speech-to-text failed with all providers.\n"
            + "\n".join(f"  - {err}" for err in errors)
            + "\n\nMake sure at least one API key is configured: "
            "OPENAI_API_KEY, DEEPGRAM_API_KEY, or GEMINI_API_KEY."
        ),
    }


def tool_text_to_speech(
    text: str,
    voice: str = "alloy",
    output_format: str = "mp3",
    provider: str = "auto",
) -> dict:
    """Generate spoken audio from text using text-to-speech.

    Converts text to speech audio and saves it to the agent's uploads folder
    for download.

    Args:
        text (str): The text to convert to speech. Maximum recommended length
                    is ~4096 characters for best results.
        voice (str): Voice to use. Options: "alloy", "echo", "fable", "onyx",
                     "nova", "shimmer". Default: "alloy".
        output_format (str): Audio output format. Options: "mp3", "opus", "aac",
                             "flac", "wav". Default: "mp3".
        provider (str): Which TTS provider to use. Options: "auto" (tries available
                        providers in order), "openai", "elevenlabs".
                        Default: "auto".

    Returns:
        dict: Contains status, file_name, download_path, base64_data,
              audio_format, content_type, size_kb, provider_used, voice,
              message, or error_message if generation fails.
    """
    if not text or not text.strip():
        return {
            "status": "error",
            "error_message": "Text input is empty. Provide text to convert to speech.",
        }

    # Determine provider order
    if provider == "auto":
        order = _TTS_PROVIDER_ORDER
    elif provider in _TTS_PROVIDERS:
        order = [provider]
    else:
        return {
            "status": "error",
            "error_message": (
                f"Unknown provider '{provider}'. "
                f"Valid: auto, {', '.join(_TTS_PROVIDERS.keys())}"
            ),
        }

    # Try each provider
    errors = []
    for prov_key in order:
        prov_name, tts_fn, env_key = _TTS_PROVIDERS[prov_key]

        if not os.environ.get(env_key):
            errors.append(f"{prov_name}: {env_key} not set")
            continue

        try:
            logger.info("[TTS] Generating with %s, voice=%s", prov_name, voice)
            audio_bytes, content_type = tts_fn(text, voice, output_format)

            # Save to uploads folder
            agent_id = os.getenv("ROOT_AGENT_ID", "")
            folder = str(agent_upload_dir(agent_id))
            os.makedirs(folder, exist_ok=True)

            slug = _sanitize_filename(text)
            ext = output_format.lower().strip(".")
            filename = f"tts_{slug}_{int(time.time())}.{ext}"
            file_path = os.path.join(folder, filename)

            with open(file_path, "wb") as f:
                f.write(audio_bytes)

            abs_path = str(Path(file_path).resolve())
            folder_name = f"agent{agent_id}"
            download_path = f"/api/files/{folder_name}/{filename}"
            b64 = base64.b64encode(audio_bytes).decode("ascii")

            logger.info(
                "[TTS] Saved %s (%d KB) via %s",
                abs_path,
                len(audio_bytes) // 1024,
                prov_name,
            )

            return {
                "status": "success",
                "file_name": filename,
                "file_path": abs_path,
                "download_path": download_path,
                "base64_data": b64,
                "audio_format": output_format.upper(),
                "content_type": content_type,
                "size_kb": round(len(audio_bytes) / 1024, 1),
                "provider_used": prov_name,
                "voice": voice,
                "message": (
                    f"Audio generated successfully with {prov_name} (voice: {voice}). "
                    f"Saved as {filename}. The user can play or download it."
                ),
            }

        except Exception as e:
            logger.warning("[TTS] %s failed: %s", prov_name, e)
            errors.append(f"{prov_name}: {e}")
            continue

    # All providers failed
    return {
        "status": "error",
        "error_message": (
            "Text-to-speech failed with all providers.\n"
            + "\n".join(f"  - {err}" for err in errors)
            + "\n\nMake sure at least one API key is configured: "
            "OPENAI_API_KEY or ELEVENLABS_API_KEY."
        ),
    }


def tool_analyze_audio(audio_file_path: str) -> dict:
    """Analyze an audio file: metadata, transcription, and enriched analysis.

    Reads file metadata, transcribes the audio content, and optionally
    performs enriched analysis (sentiment, summary, speaker count) via Gemini.

    Args:
        audio_file_path (str): Path to the audio file to analyze. Supported
                               formats: mp3, wav, ogg, m4a, webm, flac, aac, wma, opus.

    Returns:
        dict: Contains status, file_size_kb, format, transcription, summary,
              language_detected, provider_used, or error_message on failure.
    """
    # Validate file exists
    if not os.path.isfile(audio_file_path):
        return {
            "status": "error",
            "error_message": f"Audio file not found: {audio_file_path}",
        }

    # File metadata
    file_size = os.path.getsize(audio_file_path)
    ext = Path(audio_file_path).suffix.lower()
    mime = _MIME_MAP.get(ext)

    if not mime:
        return {
            "status": "error",
            "error_message": (
                f"Unsupported audio format '{ext}'. "
                f"Supported: {', '.join(sorted(_MIME_MAP.keys()))}"
            ),
        }

    result = {
        "status": "success",
        "file_size_kb": round(file_size / 1024, 1),
        "format": ext.lstrip(".").upper(),
        "mime_type": mime,
        "transcription": None,
        "summary": None,
        "language_detected": None,
        "duration_seconds": None,
        "provider_used": None,
    }

    # Attempt transcription via STT providers
    stt_result = tool_speech_to_text(audio_file_path, language="auto", provider="auto")
    if stt_result.get("status") == "success":
        result["transcription"] = stt_result.get("transcription")
        result["language_detected"] = stt_result.get("language_detected")
        result["duration_seconds"] = stt_result.get("duration_seconds")
        result["provider_used"] = stt_result.get("provider_used")

    # Enriched analysis via Gemini if available
    if os.environ.get("GEMINI_API_KEY"):
        try:
            from google import genai

            client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

            uploaded_file = client.files.upload(
                file=audio_file_path,
                config={"mime_type": mime},
            )

            analysis_prompt = (
                "Analyze this audio file and provide:\n"
                "1. A brief summary (2-3 sentences) of the content\n"
                "2. The overall sentiment (positive, negative, neutral, mixed)\n"
                "3. The estimated number of speakers\n"
                "4. Key topics discussed\n"
                "5. The language spoken\n\n"
                "Format your response as:\n"
                "Summary: ...\n"
                "Sentiment: ...\n"
                "Speakers: ...\n"
                "Topics: ...\n"
                "Language: ...\n"
            )

            response = client.models.generate_content(
                model="gemini-2.0-flash",
                contents=[uploaded_file, analysis_prompt],
            )

            if response.text:
                result["summary"] = response.text.strip()
                # Update provider if STT didn't work but Gemini analysis did
                if not result["provider_used"]:
                    result["provider_used"] = "Gemini (analysis only)"

        except Exception as e:
            logger.warning("[AUDIO_ANALYZE] Gemini enriched analysis failed: %s", e)
            # Non-critical — we still have the basic metadata and possibly transcription

    # If no transcription and no summary, note it
    if not result["transcription"] and not result["summary"]:
        result["status"] = "partial"
        result["message"] = (
            "File metadata extracted but transcription and analysis failed. "
            "Configure OPENAI_API_KEY, DEEPGRAM_API_KEY, or GEMINI_API_KEY for full analysis."
        )

    return result


def tool_generate_audio(
    prompt: str,
    duration_seconds: int = 10,
    output_format: str = "mp3",
    provider: str = "auto",
) -> dict:
    """Generate audio from a text prompt (sound effects, music, etc.).

    Note: Audio generation from text prompts is not yet available through
    stable APIs. This tool is a placeholder for future provider support.
    For spoken audio, use tool_text_to_speech instead.

    Args:
        prompt (str): Description of the audio to generate (e.g., "ocean waves
                      with seagulls", "upbeat jazz piano").
        duration_seconds (int): Desired duration in seconds. Default: 10.
        output_format (str): Audio output format. Default: "mp3".
        provider (str): Which provider to use. Options: "auto".
                        Default: "auto".

    Returns:
        dict: Info message explaining current availability status.
    """
    # Provider registry is ready but empty — providers will be added
    # as their APIs mature and stabilize.
    if provider != "auto" and provider in _AUDIOGEN_PROVIDERS:
        # Future: try the specific provider
        pass

    if provider == "auto" and _AUDIOGEN_PROVIDERS:
        # Future: iterate through available providers
        pass

    return {
        "status": "info",
        "message": (
            "Audio generation from text prompts is not yet available through "
            "stable APIs. For spoken audio, use tool_text_to_speech instead. "
            "Audio generation providers will be added as their APIs mature."
        ),
    }

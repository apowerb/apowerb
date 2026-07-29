"""Tests de la garde anti-reponse-vide du chat (proxy stream_adk_agent)."""
import os
import json
import asyncio
from unittest.mock import patch

from th2agent.core import adk_runner as AR


def _ev(text=None, fc=False, err=None):
    if err is not None:
        return "data: " + json.dumps({"error": err}) + "\n\n"
    parts = []
    if text is not None:
        parts.append({"text": text})
    if fc:
        parts.append({"functionCall": {"name": "tool_x", "args": {}}})
    return "data: " + json.dumps({"content": {"role": "model", "parts": parts}}) + "\n\n"


# --- fonction pure _stream_is_empty ---
def test_empty_string_is_empty():
    assert AR._stream_is_empty("") is True
    assert AR._stream_is_empty("   \n\n") is True


def test_only_done_is_empty():
    assert AR._stream_is_empty("data: [DONE]\n\n") is True


def test_text_is_not_empty():
    assert AR._stream_is_empty(_ev(text="Bonjour, voici la liste...")) is False


def test_function_call_is_not_empty():
    assert AR._stream_is_empty(_ev(fc=True)) is False


def test_empty_parts_is_empty():
    assert AR._stream_is_empty(_ev(text="")) is True
    assert AR._stream_is_empty(_ev()) is True


def test_error_event_is_not_empty():
    assert AR._stream_is_empty(_ev(err="boom 500")) is False
    assert AR._stream_is_empty("data: " + json.dumps({"errorCode": "MALFORMED"}) + "\n\n") is False


def test_inline_data_is_not_empty():
    # durcissement panel: une sortie image/inline ne doit PAS etre jugee vide
    ev = "data: " + json.dumps({"content": {"parts": [{"inlineData": {"mimeType": "image/png", "data": "x"}}]}}) + "\n\n"
    assert AR._stream_is_empty(ev) is False


def test_whitespace_text_is_empty():
    assert AR._stream_is_empty(_ev(text="   \n  ")) is True


def test_split_chunks_join_byte_exact():
    # chunks bruts coupes au milieu d'une ligne -> la jointure restitue le flux
    full = _ev(text="reponse coupee en deux")
    a, b = full[: len(full)//2], full[len(full)//2:]
    assert AR._stream_is_empty(a + b) is False


def test_fallback_event_shape():
    ev = AR._empty_fallback_event()
    assert ev.startswith("data: ") and ev.endswith("\n\n")
    data = json.loads(ev[6:].strip())
    assert data["content"]["parts"][0]["text"]
    assert AR._stream_is_empty(ev) is False


# --- integration stream_adk_agent ---
def _run(agen):
    async def _collect():
        return [c async for c in agen]
    return asyncio.run(_collect())


def _fake_once(chunks):
    async def gen(*, url, headers, payload):
        for c in chunks:
            yield c
    return gen


def _stream(chunks):
    with patch.object(AR, "_stream_adk_agent_once", _fake_once(chunks)), \
         patch.object(AR.settings, "root_path", "http://x"):
        return "".join(_run(AR.stream_adk_agent("agent1", "u", "s", {"parts": [{"text": "hi"}]})))


def test_stream_emits_fallback_on_empty():
    os.environ.pop("CHAT_EMPTY_FALLBACK", None)
    out = _stream([_ev(text=""), "data: [DONE]\n\n"])
    assert AR._EMPTY_FALLBACK_TEXT in out, "le repli doit etre emis sur un stream vide"


def test_no_fallback_on_content():
    os.environ.pop("CHAT_EMPTY_FALLBACK", None)
    out = _stream([_ev(text="voici la liste"), "data: [DONE]\n\n"])
    assert AR._EMPTY_FALLBACK_TEXT not in out
    assert "voici la liste" in out


def test_no_fallback_on_tool_call():
    os.environ.pop("CHAT_EMPTY_FALLBACK", None)
    out = _stream([_ev(fc=True), "data: [DONE]\n\n"])
    assert AR._EMPTY_FALLBACK_TEXT not in out


def test_killswitch_env_disables():
    os.environ["CHAT_EMPTY_FALLBACK"] = "0"
    try:
        out = _stream([_ev(text=""), "data: [DONE]\n\n"])
        assert AR._EMPTY_FALLBACK_TEXT not in out
    finally:
        os.environ.pop("CHAT_EMPTY_FALLBACK", None)


def test_large_response_no_buffering_no_fallback():
    # gros tool result (> cap) : overflow -> pas de fallback, contenu intact
    os.environ.pop("CHAT_EMPTY_FALLBACK", None)
    big = _ev(text="X" * (AR._GUARD_BUFFER_CAP + 1000))
    out = _stream([big, "data: [DONE]\n\n"])
    assert AR._EMPTY_FALLBACK_TEXT not in out
    assert "XXXX" in out  # contenu forwarde


def test_guard_exception_never_breaks_turn():
    # si la garde leve, le tour ne doit PAS casser (degradation silencieuse)
    os.environ.pop("CHAT_EMPTY_FALLBACK", None)
    with patch.object(AR, "_stream_is_empty", side_effect=ValueError("boom")):
        out = _stream([_ev(text=""), "data: [DONE]\n\n"])  # ne doit pas lever
    assert AR._EMPTY_FALLBACK_TEXT not in out  # pas de repli (garde a echoue, silencieux)

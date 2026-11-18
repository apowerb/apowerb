"""Les outils audio ne doivent pas appeler un modele Gemini arrete.

gemini-2.0-flash est arrete depuis le 01/06/2026 (page des deprecations
Google, remplacant officiel : gemini-3.6-flash). L'analyse enrichie avalait
l'erreur dans un logger.warning : la panne passait inapercue.
"""
from types import SimpleNamespace

import pytest

from apowerb.tools_store.portfolio import audio

SHUT_DOWN = {"gemini-2.0-flash", "gemini-2.0-flash-001", "gemini-2.0-flash-lite"}


class _FakeClient:
    calls: list = []

    def __init__(self, api_key=None):
        self.files = SimpleNamespace(upload=lambda file, config=None: "uploaded")
        self.models = SimpleNamespace(generate_content=self._generate)

    @classmethod
    def _generate(cls, model, contents):
        cls.calls.append(model)
        return SimpleNamespace(text="Bonjour [Language: fr]")


@pytest.fixture
def fake_gemini(monkeypatch, tmp_path):
    from google import genai

    _FakeClient.calls = []
    monkeypatch.setattr(genai, "Client", _FakeClient)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    for k in ("OPENAI_API_KEY", "DEEPGRAM_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"RIFF0000WAVE")
    return wav


def test_stt_gemini_uses_current_model(fake_gemini):
    out = audio._stt_gemini(str(fake_gemini), "fr")
    assert out["transcription"] == "Bonjour"
    assert _FakeClient.calls == [audio.GEMINI_AUDIO_MODEL]


def test_analyze_audio_uses_current_model(fake_gemini):
    out = audio.tool_analyze_audio(str(fake_gemini))
    assert out["summary"]
    assert _FakeClient.calls and set(_FakeClient.calls) == {audio.GEMINI_AUDIO_MODEL}


def test_audio_model_not_shut_down():
    assert audio.GEMINI_AUDIO_MODEL not in SHUT_DOWN
    with open(audio.__file__, encoding="utf-8") as f:
        text = f.read()
    for name in SHUT_DOWN:
        assert f'"{name}"' not in text

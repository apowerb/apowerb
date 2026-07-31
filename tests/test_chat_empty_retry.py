"""Tests du re-tirage d'un tour de chat VIDE (Fix 1, extension de la garde #212).

Un tour Gemini vide est transitoire et sans appel d'outil -> on re-emet la requete
1x (defaut) avant de tomber sur le repli. Mirroir chat du re-tirage des drafts #225.
"""
import os
import json
import asyncio
from unittest.mock import patch

from apowerb.core import adk_runner as AR


def _ev(text=None, fc=False, err=None):
    if err is not None:
        return "data: " + json.dumps({"error": err}) + "\n\n"
    parts = []
    if text is not None:
        parts.append({"text": text})
    if fc:
        parts.append({"functionCall": {"name": "tool_x", "args": {}}})
    return "data: " + json.dumps({"content": {"role": "model", "parts": parts}}) + "\n\n"


def _run(agen):
    async def _collect():
        return [c async for c in agen]
    return asyncio.run(_collect())


def _seq_once(sequences):
    """Fabrique un faux _stream_adk_agent_once qui renvoie, appel apres appel,
    la i-eme liste de chunks de `sequences` (compte les appels)."""
    calls = {"n": 0}

    async def gen(*, url, headers, payload):
        i = min(calls["n"], len(sequences) - 1)
        calls["n"] += 1
        for c in sequences[i]:
            yield c
    return gen, calls


def _stream(sequences):
    fake, calls = _seq_once(sequences)
    with patch.object(AR, "_stream_adk_agent_once", fake), \
         patch.object(AR.settings, "root_path", "http://x"):
        out = "".join(_run(AR.stream_adk_agent("agent1", "u", "s", {"parts": [{"text": "hi"}]})))
    return out, calls


def setup_function(_):
    os.environ.pop("CHAT_EMPTY_FALLBACK", None)
    os.environ.pop("CHAT_EMPTY_MAX_RETRIES", None)


def test_empty_then_content_retries_and_recovers():
    # 1er tour vide -> re-tirage -> contenu : pas de repli, l'utilisateur voit la reponse
    out, calls = _stream([[_ev(text=""), "data: [DONE]\n\n"],
                          [_ev(text="voici la liste des entreprises"), "data: [DONE]\n\n"]])
    assert "voici la liste des entreprises" in out
    assert AR._EMPTY_FALLBACK_TEXT not in out
    assert calls["n"] == 2, "exactement 1 re-tirage"


def test_empty_twice_falls_back_after_one_retry():
    # vide 2 fois de suite, defaut 1 re-tirage -> repli (2 appels au total)
    out, calls = _stream([[_ev(text=""), "data: [DONE]\n\n"],
                          [_ev(text=""), "data: [DONE]\n\n"],
                          [_ev(text="trop tard"), "data: [DONE]\n\n"]])
    assert AR._EMPTY_FALLBACK_TEXT in out
    assert calls["n"] == 2, "1 tour + 1 re-tirage, pas plus"


def test_content_first_no_retry():
    out, calls = _stream([[_ev(text="direct"), "data: [DONE]\n\n"]])
    assert "direct" in out and AR._EMPTY_FALLBACK_TEXT not in out
    assert calls["n"] == 1, "aucun re-tirage si le 1er tour porte du contenu"


def test_retries_disabled_keeps_legacy_fallback():
    os.environ["CHAT_EMPTY_MAX_RETRIES"] = "0"
    out, calls = _stream([[_ev(text=""), "data: [DONE]\n\n"],
                          [_ev(text="jamais lu"), "data: [DONE]\n\n"]])
    assert AR._EMPTY_FALLBACK_TEXT in out
    assert "jamais lu" not in out
    assert calls["n"] == 1, "0 re-tirage -> repli direct (comportement historique)"


def test_more_retries_env():
    os.environ["CHAT_EMPTY_MAX_RETRIES"] = "2"
    out, calls = _stream([[_ev(text="")], [_ev(text="")], [_ev(text="enfin")]])
    assert "enfin" in out and AR._EMPTY_FALLBACK_TEXT not in out
    assert calls["n"] == 3, "2 re-tirages autorises"


def test_tool_call_first_never_retries():
    out, calls = _stream([[_ev(fc=True), "data: [DONE]\n\n"]])
    assert AR._EMPTY_FALLBACK_TEXT not in out
    assert calls["n"] == 1


def test_killswitch_disables_everything():
    os.environ["CHAT_EMPTY_FALLBACK"] = "0"
    try:
        out, calls = _stream([[_ev(text=""), "data: [DONE]\n\n"],
                              [_ev(text="x"), "data: [DONE]\n\n"]])
        assert AR._EMPTY_FALLBACK_TEXT not in out
        assert calls["n"] == 1, "garde off -> ni re-tirage ni repli"
    finally:
        os.environ.pop("CHAT_EMPTY_FALLBACK", None)


def test_rate_limit_during_redraw_surfaces_error_no_fallback():
    rl = "data: " + json.dumps({"error": "litellm.RateLimitError: 429 RESOURCE_EXHAUSTED"}) + "\n\n"
    out, calls = _stream([[_ev(text=""), "data: [DONE]\n\n"], [rl]])
    assert "RateLimitError" in out
    assert AR._EMPTY_FALLBACK_TEXT not in out, "pas de repli blanc par-dessus une erreur"

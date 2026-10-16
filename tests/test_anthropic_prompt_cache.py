"""Cache de prompt Claude : points d'injection LiteLLM posés sur les modèles anthropic/.

Sans marqueur ``cache_control``, Anthropic ne met RIEN en cache : chaque appel d'un tour
repaie l'en-tête fixe (outils + instruction, 5 à 17 k jetons) au plein tarif. Le client
demande donc à LiteLLM de marquer le message système (qui couvre aussi les outils, placés
avant lui dans l'ordre de cache d'Anthropic) et le dernier message (la conversation).
"""
import json

import pytest

from apowerb.core.agent_helpers.anthropic_prompt_cache import (
    CACHE_POINTS,
    with_cache_points,
)
from apowerb.core.agent_helpers.reasoning_strip import CleaningLiteLLMClient

FLAG = "APOWERB_ANTHROPIC_PROMPT_CACHE"
CLAUDE = "anthropic/claude-sonnet-4-5-20250929"


def _client(echecs=0):
    appels, restant = [], [echecs]

    class _Enregistreur(CleaningLiteLLMClient):
        async def _appel_reel(self, **kwargs):
            appels.append(kwargs)
            if restant[0]:
                restant[0] -= 1
                raise RuntimeError("cache_control refuse")
            return "reponse"

    return _Enregistreur(), appels


def _kwargs(model):
    return {"model": model, "messages": [{"role": "system", "content": "x"}, {"role": "user", "content": "y"}]}


async def test_claude_recoit_les_points_par_defaut(monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    client, appels = _client()
    await client.acompletion(**_kwargs(CLAUDE))
    assert appels[0]["cache_control_injection_points"] == CACHE_POINTS
    assert {"location": "message", "role": "system"} in CACHE_POINTS
    assert {"location": "message", "index": -1} in CACHE_POINTS


@pytest.mark.parametrize(
    "model", ["gemini/gemini-2.5-flash", "openai/gpt-6-luna", "ovhcloud/Qwen2.5-VL", "groq/openai/gpt-oss-20b", ""]
)
async def test_autres_fournisseurs_intacts(monkeypatch, model):
    monkeypatch.delenv(FLAG, raising=False)
    client, appels = _client()
    await client.acompletion(**_kwargs(model))
    assert "cache_control_injection_points" not in appels[0]


@pytest.mark.parametrize("valeur", ["0", "false", "no", "off"])
async def test_drapeau_coupe(monkeypatch, valeur):
    monkeypatch.setenv(FLAG, valeur)
    client, appels = _client()
    await client.acompletion(**_kwargs(CLAUDE))
    assert "cache_control_injection_points" not in appels[0]


async def test_points_deja_fournis_respectes(monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    mien = [{"location": "message", "index": 0}]
    client, appels = _client()
    await client.acompletion(**_kwargs(CLAUDE), cache_control_injection_points=mien)
    assert appels[0]["cache_control_injection_points"] is mien


async def test_repli_sans_cache_si_le_fournisseur_refuse(monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    client, appels = _client(echecs=1)
    assert await client.acompletion(**_kwargs(CLAUDE)) == "reponse"
    assert len(appels) == 2
    assert "cache_control_injection_points" in appels[0]
    assert "cache_control_injection_points" not in appels[1]


async def test_deux_echecs_remontent(monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    client, _ = _client(echecs=2)
    with pytest.raises(RuntimeError):
        await client.acompletion(**_kwargs(CLAUDE))


def test_chaque_appel_recoit_sa_propre_liste(monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    a = with_cache_points(_kwargs(CLAUDE))["cache_control_injection_points"]
    a[0]["role"] = "user"
    assert with_cache_points(_kwargs(CLAUDE))["cache_control_injection_points"] == CACHE_POINTS
    assert CACHE_POINTS[0]["role"] == "system"


def test_kwargs_non_mutes(monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    kwargs = _kwargs(CLAUDE)
    out = with_cache_points(kwargs)
    assert "cache_control_injection_points" not in kwargs
    assert out is not kwargs


def test_litellm_pose_les_marqueurs_dans_la_requete_anthropic(monkeypatch):
    """Le corps HTTP réellement produit par LiteLLM porte cache_control sur le système
    et sur le dernier message : sans ce contrôle, un point mal nommé passerait en silence."""
    import litellm

    monkeypatch.delenv(FLAG, raising=False)
    captured = {}

    def fake_post(*args, **kwargs):
        captured["body"] = json.loads(kwargs["data"])
        raise RuntimeError("stop")

    from litellm.llms.custom_httpx.http_handler import HTTPHandler

    monkeypatch.setattr(HTTPHandler, "post", fake_post)
    kwargs = with_cache_points(
        {
            "model": CLAUDE,
            "messages": [
                {"role": "system", "content": "SYSTEM " * 50},
                {"role": "user", "content": "bonjour"},
            ],
        }
    )
    with pytest.raises(Exception):
        litellm.completion(**kwargs, api_key="fake", api_base="http://127.0.0.1:9", client=HTTPHandler())
    body = captured["body"]
    assert any("cache_control" in b for b in body["system"])
    assert any("cache_control" in c for c in body["messages"][-1]["content"])

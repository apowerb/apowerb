"""Cache de prompt explicite Gemini, derriere un drapeau DESACTIVE par defaut.

Le prefixe fixe d'un agent (instruction systeme + outils) pesait 98,7 % d'une requete
de production mesuree le 17/09/2026, et n'etait mis en cache que 22 % du temps, par le
cache implicite de Gemini. Ce module pose le marqueur ``cache_control`` que LiteLLM sait
transformer en cache explicite -- et strictement rien tant que le drapeau est eteint.
"""
import pytest

from apowerb.core.agent_helpers.gemini_prompt_cache import (
    explicit_cache_enabled,
    mark_system_prefix_for_cache,
)
from apowerb.core.agent_helpers.reasoning_strip import CleaningLiteLLMClient

FLAG = "APOWERB_GEMINI_EXPLICIT_CACHE"
TTL = "APOWERB_GEMINI_CACHE_TTL_SECONDS"
GEMINI = "gemini/gemini-2.5-flash"


def _messages():
    return [
        {"role": "system", "content": "Tu es un agent. " * 50},
        {"role": "user", "content": "Compte de 1 a 20."},
    ]


def _porte_un_marqueur(messages):
    for m in messages:
        if isinstance(m.get("cache_control"), dict):
            return True
        c = m.get("content")
        if isinstance(c, list) and any(isinstance(b, dict) and "cache_control" in b for b in c):
            return True
    return False


def _client(echecs=0):
    """Client reel dont seul l'appel reseau est remplace. Pas d'attribut d'instance :
    le client herite d'une classe d'ADK dont on ne presume pas la structure."""
    appels, restant = [], [echecs]

    class _Enregistreur(CleaningLiteLLMClient):
        async def _appel_reel(self, **kwargs):
            appels.append(kwargs)
            if restant[0]:
                restant[0] -= 1
                raise RuntimeError("cachedContent refuse")
            return "reponse"

    return _Enregistreur(), appels


# --- Drapeau eteint : comportement STRICTEMENT inchange ---------------------

@pytest.mark.parametrize("valeur", [None, "", "0", "false", "no", "off", "False"])
def test_drapeau_eteint_par_defaut(monkeypatch, valeur):
    if valeur is None:
        monkeypatch.delenv(FLAG, raising=False)
    else:
        monkeypatch.setenv(FLAG, valeur)
    assert explicit_cache_enabled() is False


def test_drapeau_eteint_renvoie_la_meme_liste(monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    msgs = _messages()
    assert mark_system_prefix_for_cache(GEMINI, msgs) is msgs


@pytest.mark.asyncio
async def test_drapeau_eteint_le_client_n_envoie_aucun_marqueur(monkeypatch):
    """Le chemin de production par defaut, de bout en bout, a travers la vraie classe."""
    monkeypatch.delenv(FLAG, raising=False)
    client, appels = _client()
    await client.acompletion(model=GEMINI, messages=_messages())
    assert len(appels) == 1
    assert not _porte_un_marqueur(appels[0]["messages"])


@pytest.mark.asyncio
async def test_drapeau_eteint_aucune_nouvelle_tentative(monkeypatch):
    """Drapeau eteint : une erreur remonte telle quelle, sans deuxieme appel. Sinon on
    doublerait en silence les appels -- et la facture -- de la prod."""
    monkeypatch.delenv(FLAG, raising=False)
    client, appels = _client(echecs=1)
    with pytest.raises(RuntimeError):
        await client.acompletion(model=GEMINI, messages=_messages())
    assert len(appels) == 1


# --- Drapeau allume ---------------------------------------------------------

def test_marque_le_message_systeme_sans_muter_l_original(monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    monkeypatch.delenv(TTL, raising=False)
    msgs = _messages()
    texte = msgs[0]["content"]
    out = mark_system_prefix_for_cache(GEMINI, msgs)

    assert out is not msgs
    assert msgs[0]["content"] == texte, "l'original ne doit pas etre modifie"
    assert out[0]["role"] == "system"
    assert out[0]["content"] == [{
        "type": "text",
        "text": texte,
        "cache_control": {"type": "ephemeral", "ttl": "3600s"},
    }]
    assert out[1] == msgs[1], "les messages non systeme passent tels quels"


def test_litellm_reconnait_le_marqueur_et_son_ttl(monkeypatch):
    """Le test qui compte : c'est LiteLLM, pas moi, qui doit lire ce marqueur. S'il en
    change le format a une montee de version, c'est ici que ca cassera."""
    from litellm.llms.vertex_ai.context_caching.transformation import (
        extract_ttl_from_cached_messages,
    )
    from litellm.utils import is_cached_message

    monkeypatch.setenv(FLAG, "true")
    monkeypatch.setenv(TTL, "600")
    out = mark_system_prefix_for_cache(GEMINI, _messages())
    assert is_cached_message(out[0]) is True
    assert is_cached_message(out[1]) is False
    assert extract_ttl_from_cached_messages(out) == "600s"


@pytest.mark.parametrize("modele", [
    "openai/Mistral-Small-3.2-24B-Instruct-2506",
    "anthropic/claude-sonnet",
    "ovhcloud/Qwen3-32B",
])
def test_modele_non_gemini_intact(monkeypatch, modele):
    monkeypatch.setenv(FLAG, "1")
    msgs = _messages()
    assert mark_system_prefix_for_cache(modele, msgs) is msgs


def test_sans_message_systeme_intact(monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    msgs = [{"role": "user", "content": "bonjour"}]
    assert mark_system_prefix_for_cache(GEMINI, msgs) is msgs


@pytest.mark.parametrize("valeur", ["abc", "0", "-5", ""])
def test_ttl_invalide_retombe_sur_une_heure(monkeypatch, valeur):
    monkeypatch.setenv(FLAG, "1")
    monkeypatch.setenv(TTL, valeur)
    out = mark_system_prefix_for_cache(GEMINI, _messages())
    assert out[0]["content"][-1]["cache_control"]["ttl"] == "3600s"


def test_contenu_systeme_deja_en_blocs(monkeypatch):
    """Un message systeme deja en blocs garde ses blocs ; le marqueur va sur le DERNIER
    bloc texte, pour couvrir tout le prefixe."""
    monkeypatch.setenv(FLAG, "1")
    monkeypatch.delenv(TTL, raising=False)
    msgs = [
        {"role": "system", "content": [
            {"type": "text", "text": "partie A"},
            {"type": "text", "text": "partie B"},
        ]},
        {"role": "user", "content": "q"},
    ]
    out = mark_system_prefix_for_cache(GEMINI, msgs)
    blocs = out[0]["content"]
    assert "cache_control" not in blocs[0]
    assert blocs[1]["cache_control"] == {"type": "ephemeral", "ttl": "3600s"}
    assert "cache_control" not in msgs[0]["content"][1], "original non mute"


# --- Repli : le cache ne doit jamais casser une reponse -----------------------

@pytest.mark.asyncio
async def test_echec_avec_cache_repli_sans_cache(monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    client, appels = _client(echecs=1)
    assert await client.acompletion(model=GEMINI, messages=_messages()) == "reponse"
    assert len(appels) == 2
    assert _porte_un_marqueur(appels[0]["messages"]) is True
    assert _porte_un_marqueur(appels[1]["messages"]) is False


@pytest.mark.asyncio
async def test_une_vraie_panne_n_est_pas_masquee(monkeypatch):
    """Si l'appel SANS cache echoue aussi, ce n'est pas le cache : l'erreur remonte."""
    monkeypatch.setenv(FLAG, "1")
    client, appels = _client(echecs=2)
    with pytest.raises(RuntimeError):
        await client.acompletion(model=GEMINI, messages=_messages())
    assert len(appels) == 2

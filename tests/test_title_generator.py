"""Tests unitaires du générateur de titre de conversation.

Aucun accès réseau / DB : ``litellm.acompletion`` et ``load_agent_model_params``
sont mockés. ``asyncio.run`` évite de dépendre de la config pytest-asyncio.
"""

import asyncio
import types

import pytest

from th2agent.helpers import title_generator as tg


# ── clean_title ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "raw,expected",
    [
        ('"Analyse des ventes Q2"', "Analyse des ventes Q2"),
        ("Titre : Rapport mensuel", "Rapport mensuel"),
        ("Requête SQL clients.", "Requête SQL clients"),
        ("Plan\nblabla en trop", "Plan"),
        ("  Bonjour le monde  ", "Bonjour le monde"),
        ("", ""),
        (None, ""),
    ],
)
def test_clean_title(raw, expected):
    assert tg.clean_title(raw) == expected


def test_clean_title_borne_mots():
    raw = "un deux trois quatre cinq six sept huit neuf dix"
    assert len(tg.clean_title(raw).split()) <= 8


def test_fallback_title_premiers_mots():
    assert tg._fallback_title("Peux-tu me sortir les ventes de juin par région stp")  # non vide
    assert tg._fallback_title("") == "Nouvelle conversation"
    assert tg._fallback_title("   ") == "Nouvelle conversation"


# ── generate_session_title ──────────────────────────────────────────────────
def test_generate_message_vide_renvoie_defaut():
    assert asyncio.run(tg.generate_session_title("", agent_id=5)) == "Nouvelle conversation"


def test_generate_sans_agent_retombe_sur_fallback(monkeypatch):
    # Pas d'agent -> pas de creds -> fallback déterministe, aucun appel LLM.
    called = {"llm": False}

    async def _boom(**_):
        called["llm"] = True
        raise AssertionError("litellm ne doit pas être appelé sans creds")

    monkeypatch.setattr("litellm.acompletion", _boom, raising=False)
    out = asyncio.run(tg.generate_session_title("Analyse mes ventes", agent_id=None))
    assert out == "Analyse mes ventes"
    assert called["llm"] is False


def test_generate_avec_llm_ok(monkeypatch):
    monkeypatch.setattr(
        tg, "load_agent_model_params",
        lambda _id: ("mistral/Mistral-Small", {"model_api_key": "k", "model_api_base": None}),
    )

    async def _fake_acompletion(**_):
        msg = types.SimpleNamespace(content='"Ventes juin par région"')
        choice = types.SimpleNamespace(message=msg)
        return types.SimpleNamespace(choices=[choice])

    import litellm
    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion, raising=False)

    out = asyncio.run(tg.generate_session_title("sors-moi les ventes de juin par région", agent_id="agent7"))
    assert out == "Ventes juin par région"


def test_generate_llm_en_erreur_retombe_sur_fallback(monkeypatch):
    monkeypatch.setattr(
        tg, "load_agent_model_params",
        lambda _id: ("mistral/Mistral-Small", {"model_api_key": "k", "model_api_base": None}),
    )

    async def _raise(**_):
        raise RuntimeError("LLM down")

    import litellm
    monkeypatch.setattr(litellm, "acompletion", _raise, raising=False)

    out = asyncio.run(tg.generate_session_title("Bonjour ceci est un test", agent_id=7))
    assert out == "Bonjour ceci est un test"

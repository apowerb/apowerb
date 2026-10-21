"""Un modele ``edenai/<provider>/<nom>`` part chez Eden AI avec son identifiant entier.

Eden AI est une passerelle : ses identifiants sont deja de la forme
``provider/modele``. Le prefixe ``edenai/`` est une sentinelle, comme
``thaink2/default`` : elle porte l'endpoint par defaut et disparait avant
l'appel, sans entamer l'identifiant qui suit.
"""

from __future__ import annotations

import pytest

from apowerb.core.agent_helpers import llm_model_builder as builder

EDENAI_BASE = "https://api.edenai.run/v3"


@pytest.fixture(autouse=True)
def _no_encryption(monkeypatch):
    monkeypatch.setattr(builder, "decrypt_value_in_dict", lambda d, **_: d)


@pytest.fixture
def captured(monkeypatch):
    seen: dict = {}

    def fake_litellm(**kwargs):
        seen.update(kwargs)
        return type("Model", (), kwargs)()

    monkeypatch.setattr(builder, "LiteLlm", fake_litellm)
    return seen


def test_an_edenai_model_reaches_eden_ai_with_its_provider(captured):
    builder.build_litellm_model(
        {
            "agent_model": "edenai/anthropic/claude-haiku-5-5",
            "agent_model_params": {"model_api_key": "sk-eden"},
        },
        temperature=None,
    )

    # litellm retire le seul prefixe openai/, donc Eden AI recoit
    # anthropic/claude-haiku-5-5 et route vers le bon provider.
    assert captured["model"] == "openai/anthropic/claude-haiku-5-5"
    assert captured["api_base"] == EDENAI_BASE
    assert captured["api_key"] == "sk-eden"


def test_a_three_segment_edenai_model_is_not_cut(captured):
    builder.build_litellm_model(
        {
            "agent_model": "edenai/infomaniak/google/gemma-4-31B-it",
            "agent_model_params": {"model_api_key": "sk-eden"},
        },
        temperature=None,
    )

    assert captured["model"] == "openai/infomaniak/google/gemma-4-31B-it"


def test_an_explicit_endpoint_wins_over_the_default(captured):
    """Un compte sur la region EU doit pouvoir pointer son propre endpoint."""
    builder.build_litellm_model(
        {
            "agent_model": "edenai/anthropic/claude-haiku-5-5",
            "agent_model_params": {
                "model_api_key": "sk-eden",
                "model_api_base": "https://api.eu.edenai.run/v3",
            },
        },
        temperature=None,
    )

    assert captured["api_base"] == "https://api.eu.edenai.run/v3"
    assert captured["model"] == "openai/anthropic/claude-haiku-5-5"


def test_an_edenai_agent_is_accepted_at_creation_without_an_endpoint():
    """Sans cette garde, litellm ne connait pas `edenai` et l'agent est refuse."""
    builder.validate_agent_model(
        "edenai/openai/gpt-4o-mini", {"model_api_key": "sk-eden"}
    )


def test_an_unknown_provider_is_still_refused():
    with pytest.raises(ValueError):
        builder.validate_agent_model("ovh/Qwen3.5", {})

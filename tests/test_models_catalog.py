"""Le catalogue servi par GET /models : premier modèle = choix recommandé.

Cliquer sur un fournisseur dans le sélecteur choisit son premier modèle. Un
premier modèle en preview ou périmé oblige l'utilisateur à passer par
« Custom model » à chaque création d'agent.
"""

import asyncio

from apowerb.configs.models import MODELS
from apowerb.routers.models import get_models


def _catalog():
    return [g for g in asyncio.run(get_models())["providers"] if g["provider"] != "thaink2"]


def test_ids_are_unique_and_prefixed_by_their_provider():
    ids = [m["id"] for m in MODELS]
    assert len(ids) == len(set(ids))
    for m in MODELS:
        assert m["id"].startswith(f"{m['provider']}/"), m["id"]


def test_first_model_of_each_provider_is_the_recommended_one():
    groups = _catalog()
    assert {g["provider"] for g in groups} == {
        "anthropic", "openai", "mistral", "gemini", "deepseek", "groq", "edenai", "azure_ai",
    }
    for group in groups:
        tags = [m["tag"] for m in group["models"]]
        assert tags[0] == "Recommended", (group["provider"], tags)
        assert tags.count("Recommended") == 1, (group["provider"], tags)


def test_gemini_defaults_to_a_stable_model_not_a_preview():
    gemini = next(g for g in _catalog() if g["provider"] == "gemini")
    assert not gemini["models"][0]["id"].endswith("-preview")


def test_edenai_models_route_to_eden_without_a_configured_endpoint():
    # Choisir Eden AI dans le sélecteur ne demande que la clé : l'URL Eden
    # est ajoutée par défaut et le provider amont arrive entier.
    from apowerb.helpers.model_names import (
        EDENAI_API_BASE,
        default_api_base,
        openai_compat_model_name,
    )

    eden = next(g for g in _catalog() if g["provider"] == "edenai")
    for m in eden["models"]:
        upstream = m["id"][len("edenai/"):]
        assert default_api_base(m["id"]) == EDENAI_API_BASE, m["id"]
        assert openai_compat_model_name(m["id"]) == f"openai/{upstream}", m["id"]


def test_azure_ai_models_keep_their_native_prefix():
    # Azure AI Foundry a son implementation litellm : avec l'URL du projet,
    # le nom part tel quel, sans repasser par le chemin OpenAI.
    from apowerb.helpers.model_names import openai_compat_model_name

    azure = next(g for g in _catalog() if g["provider"] == "azure_ai")
    for m in azure["models"]:
        assert openai_compat_model_name(m["id"]) == m["id"], m["id"]


def test_azure_ai_agent_needs_its_project_url(monkeypatch):
    # Pas d'URL par defaut : chaque client a son projet Foundry. Sans URL
    # l'agent serait cree puis muet a chaque tour.
    import pytest

    from apowerb.core.agent_helpers.llm_model_builder import validate_agent_model

    monkeypatch.delenv("AZURE_AI_API_BASE", raising=False)
    with pytest.raises(ValueError, match="Azure AI Foundry"):
        validate_agent_model("azure_ai/gpt-6.1-sol", {})
    validate_agent_model(
        "azure_ai/gpt-6.1-sol",
        {"model_api_base": "https://mon-projet.services.ai.azure.com/models"},
    )
    monkeypatch.setenv("AZURE_AI_API_BASE", "https://serveur.services.ai.azure.com/models")
    validate_agent_model("azure_ai/gpt-6.1-sol", {})

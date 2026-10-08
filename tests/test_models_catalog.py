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
        "anthropic", "openai", "mistral", "gemini", "deepseek", "groq",
    }
    for group in groups:
        tags = [m["tag"] for m in group["models"]]
        assert tags[0] == "Recommended", (group["provider"], tags)
        assert tags.count("Recommended") == 1, (group["provider"], tags)


def test_gemini_defaults_to_a_stable_model_not_a_preview():
    gemini = next(g for g in _catalog() if g["provider"] == "gemini")
    assert not gemini["models"][0]["id"].endswith("-preview")

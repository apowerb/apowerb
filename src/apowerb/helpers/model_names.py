"""Nom de modele a passer a litellm quand on appelle un endpoint OpenAI-compatible.

Un ``api_base`` custom parle le protocole OpenAI, donc litellm doit prendre son
chemin OpenAI plutot que le SDK du provider d'origine. Les providers qui ont
leur propre implementation litellm gardent en revanche leur prefixe : les
router vers openai casserait l'authentification et le format de requete.
"""

from __future__ import annotations

# Providers qui ont leur propre implementation litellm et gardent leur prefixe.
NATIVE_PREFIXES = ("openai/", "azure_ai/")


def openai_compat_model_name(model: str) -> str:
    """Retourne le nom de modele a donner a litellm pour un endpoint OpenAI-compatible.

    ``mistral/Mistral-Small`` devient ``openai/Mistral-Small``, alors que
    ``azure_ai/mistral-large`` et ``openai/gpt-4o`` sont laisses intacts.
    """
    if model.startswith(NATIVE_PREFIXES):
        return model
    return "openai/" + model.split("/", 1)[-1]

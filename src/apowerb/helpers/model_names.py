"""Nom de modele a passer a litellm quand on appelle un endpoint OpenAI-compatible.

Un ``api_base`` custom parle le protocole OpenAI, donc litellm doit prendre son
chemin OpenAI plutot que le SDK du provider d'origine. Les providers qui ont
leur propre implementation litellm gardent en revanche leur prefixe : les
router vers openai casserait l'authentification et le format de requete.
"""

from __future__ import annotations

# Providers qui ont leur propre implementation litellm et gardent leur prefixe.
NATIVE_PREFIXES = ("openai/", "azure_ai/")

# Passerelle Eden AI. ``edenai/`` est une sentinelle, comme ``thaink2/default`` :
# elle porte l'endpoint par defaut et disparait avant l'appel.
EDENAI_PREFIX = "edenai/"
EDENAI_API_BASE = "https://api.edenai.run/v3"

# Endpoint OVH du Mistral-Small. Ne vise que les noms du catalogue OVH : les ids
# de l'API Mistral sont en minuscules (``mistral/mistral-large-latest``) et vont
# chez Mistral avec la cle de l'agent.
OVH_MISTRAL_PREFIX = "mistral/Mistral-"
OVH_MISTRAL_API_BASE = (
    "https://mistral-small-3-2-24b-instruct-2506.endpoints.kepler.ai.cloud.ovh.net"
    "/api/openai_compat/v1"
)


def is_edenai_model(model: str) -> bool:
    """Vrai pour un identifiant de la passerelle Eden AI."""
    return model.startswith(EDENAI_PREFIX)


def default_api_base(model: str) -> str | None:
    """Endpoint que le prefixe du modele implique, ou ``None`` s'il n'en implique aucun.

    Un ``model_api_base`` pose sur l'agent reste prioritaire : l'appelant ne
    consulte cette fonction que lorsqu'il n'en a pas, d'ou la forme
    ``params.get("model_api_base") or default_api_base(model)``.
    """
    if is_edenai_model(model):
        return EDENAI_API_BASE
    if model.startswith(OVH_MISTRAL_PREFIX):
        return OVH_MISTRAL_API_BASE
    return None


def openai_compat_model_name(model: str) -> str:
    """Retourne le nom de modele a donner a litellm pour un endpoint OpenAI-compatible.

    ``mistral/Mistral-Small`` devient ``openai/Mistral-Small``, alors que
    ``azure_ai/mistral-large`` et ``openai/gpt-4o`` sont laisses intacts.

    Un identifiant Eden AI est deja de la forme ``provider/modele`` : on retire
    la sentinelle et on garde le reste entier, sans decoupe. ``edenai/`` plus
    ``anthropic/claude-haiku-5-5`` donne ``openai/anthropic/claude-haiku-5-5``,
    dont litellm retire le seul ``openai/`` avant d'appeler l'endpoint.
    """
    if is_edenai_model(model):
        return "openai/" + model[len(EDENAI_PREFIX) :]
    if model.startswith(NATIVE_PREFIXES):
        return model
    return "openai/" + model.split("/", 1)[-1]

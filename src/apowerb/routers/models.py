from fastapi import APIRouter
from apowerb.configs.models import MODELS
from apowerb.core.agent_helpers.default_llm import (
    DEFAULT_LLM_MODEL_ID,
    DEFAULT_LLM_PROVIDER,
    default_llm_available,
)

router = APIRouter()


@router.get("/models")
async def get_models():
    grouped_models = {}

    for model in MODELS:
        provider = model["provider"]

        if provider not in grouped_models:
            grouped_models[provider] = []

        grouped_models[provider].append(
            {
                "id": model["id"],
                "name": model["name"],
                "tag": model.get("tag"),
            }
        )

    providers = [
        {
            "provider": provider,
            "models": models,
        }
        for provider, models in grouped_models.items()
    ]

    # Modele mutualise thaink2 : en tete de liste, et seulement si le serveur
    # le sert vraiment (DEFAULT_LLM_MODEL + DEFAULT_LLM_API_KEY). Le front n'a
    # donc aucun flag a interpreter -- l'option existe ou n'existe pas.
    # `requires_api_key: False` est ce qui permet a l'UI de masquer le champ
    # cle : c'est tout l'interet, l'utilisateur n'a rien a saisir.
    if default_llm_available():
        providers.insert(
            0,
            {
                "provider": DEFAULT_LLM_PROVIDER,
                "requires_api_key": False,
                "models": [
                    {
                        "id": DEFAULT_LLM_MODEL_ID,
                        "name": "thaink2 (inclus)",
                        "tag": "Default",
                    }
                ],
            },
        )

    return {"providers": providers}

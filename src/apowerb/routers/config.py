"""Public configuration endpoint."""

from fastapi import APIRouter
from apowerb.core.agent_helpers.default_llm import (
    DEFAULT_LLM_MODEL_ID,
    default_llm_available,
)
from apowerb.core.extensions.registry import registry as _registry

router = APIRouter(tags=["config"])


@router.get("/config")
async def get_public_config():
    """Return public configuration (no auth required)."""
    return {
        # Disponibilite du modele mutualise -- jamais sa cle ni le modele
        # sous-jacent : cet endpoint est public (sans auth).
        "default_llm_available": default_llm_available(),
        "default_llm_model_id": DEFAULT_LLM_MODEL_ID,
        # Drapeaux apportes par les briques branchees. `billing_enabled` vivait
        # ici en dur ; c'est desormais la brique de facturation qui l'annonce,
        # donc la cle disparait purement et simplement quand elle n'est pas la.
        **_registry.feature_flags(),
    }

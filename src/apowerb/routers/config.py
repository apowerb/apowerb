"""Public configuration endpoint."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from apowerb.auth.dependencies import get_current_user
from apowerb.core.agent_helpers.default_llm import (
    DEFAULT_LLM_MODEL_ID,
    default_llm_available,
)
from apowerb.core.agent_helpers.default_llm_usage import (
    DefaultLlmUsage,
    default_llm_usage,
)
from apowerb.core.setup_status import SetupStatus, setup_status
from apowerb.core.workflow_suggest import suggest_enabled
from apowerb.core.extensions.registry import registry as _registry
from apowerb.helpers.ownership import is_admin
from apowerb.helpers.database import get_db
from apowerb.users import schemas as user_schemas

router = APIRouter(tags=["config"])


@router.get("/config")
async def get_public_config():
    """Return public configuration (no auth required)."""
    return {
        # Disponibilite du modele mutualise -- jamais sa cle ni le modele
        # sous-jacent : cet endpoint est public (sans auth).
        "default_llm_available": default_llm_available(),
        "default_llm_model_id": DEFAULT_LLM_MODEL_ID,
        # L'éditeur de workflows n'affiche le bouton ✨ que si c'est vrai.
        "workflow_suggest_enabled": suggest_enabled(),
        # Drapeaux apportes par les briques branchees. `billing_enabled` vivait
        # ici en dur ; c'est desormais la brique de facturation qui l'annonce,
        # donc la cle disparait purement et simplement quand elle n'est pas la.
        **_registry.feature_flags(),
    }


@router.get("/config/default-llm/usage", response_model=DefaultLlmUsage)
async def get_default_llm_usage(
    current_user: user_schemas.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DefaultLlmUsage:
    """Consommation du mois de l'utilisateur sur le modele mutualise.

    Authentifie, contrairement a ``GET /config`` : le compteur est celui du
    demandeur (``llm_usage.owner_id``), jamais celui d'un autre. Sert la
    jauge affichee a la place de la cle API quand un agent est en
    ``thaink2/default`` ; ``enabled=False`` quand ce serveur ne propose pas
    de modele mutualise -- il n'y a alors rien a compter.
    """
    return await default_llm_usage(
        db, owner_id=current_user.email, plan=current_user.plan
    )


@router.get("/config/setup", response_model=SetupStatus)
async def get_setup_status(
    current_user: user_schemas.User = Depends(get_current_user),
) -> SetupStatus:
    """Which features are configured on this server, and -- for an
    administrator -- which variables are still missing. Never a value.

    Everyone authenticated sees the flags: that is what lets a screen say
    « not configured yet, contact your administrator » instead of failing.
    Only an administrator sees the variable names, since fixing it is theirs.
    """
    return setup_status(for_admin=is_admin(current_user))

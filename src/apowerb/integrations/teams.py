"""Intégration Microsoft Teams — webhook entrant (Workflows / Power Automate).

Contrairement aux intégrations OAuth (Microsoft/Google), Teams ici n'est
qu'une URL de webhook entrant fournie par l'utilisateur (Workflows, ex
"Incoming Webhook" historique de Teams, ou un flux Power Automate /
Power Platform exposant la même forme de endpoint). Elle est stockée
chiffrée dans ``Integration.access_token`` sous le provider
``teams_webhook`` — une par utilisateur, même schéma que ``odoo.py`` — et
n'est jamais renvoyée en clair une fois enregistrée, ni écrite dans un
graphe de workflow (cf. ``core.workflow_runtime._notify_teams``).

Validée à l'enregistrement ET à l'envoi (defense in depth, ``core.
workflow_runtime`` revalide avant chaque POST) :

* https uniquement ;
* hôte dans une liste blanche de suffixes Microsoft, en SUFFIXE DE DOMAINE
  EXACT — ``evil-webhook.office.com.attacker.com`` ne doit pas passer parce
  que la chaîne contient ``webhook.office.com`` quelque part ;
* pas d'IP interne/privée, via la garde SSRF de ``routers/rag/validators``
  (résolution DNS incluse : même raisonnement que le nœud ``http``).
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apowerb.helpers.encryptor import decrypt_value, encrypt_value
from apowerb.models import Integration

TEAMS_WEBHOOK_PROVIDER = "teams_webhook"

# Suffixes acceptés pour l'hôte du webhook (avec le point de tête : un hôte
# doit soit être EXACTEMENT le nom nu, soit se terminer par ``.<nom nu>``).
_ALLOWED_HOST_SUFFIXES = (
    ".webhook.office.com",
    ".logic.azure.com",
    ".environment.api.powerplatform.com",
    ".api.powerplatform.com",
)


class TeamsWebhookRefused(Exception):
    """URL de webhook Teams refusée (forme, hôte hors liste blanche, ou SSRF).

    Jamais d'``HTTPException`` ici : ce module est appelé aussi bien depuis
    le routeur HTTP (qui traduit en 422) que depuis l'exécution du graphe
    (qui traduit en ``GraphError`` / code produit).
    """


def _host_allowed(hostname: str) -> bool:
    host = (hostname or "").lower().rstrip(".")
    for suffix in _ALLOWED_HOST_SUFFIXES:
        bare = suffix.lstrip(".")
        if host == bare or host.endswith(suffix):
            return True
    return False


def validate_teams_webhook_url(url: str) -> str:
    """``url`` si c'est un webhook Teams/Power-Automate plausible et non
    interne, sinon lève ``TeamsWebhookRefused``."""
    from fastapi import HTTPException

    from apowerb.routers.rag.validators import _validate_url_not_internal

    parsed = urlparse(url or "")
    if parsed.scheme != "https":
        raise TeamsWebhookRefused("https requis")
    if not _host_allowed(parsed.hostname or ""):
        raise TeamsWebhookRefused(
            "hôte non autorisé (attendu un webhook Teams / Power Automate / "
            "Power Platform)"
        )
    try:
        return _validate_url_not_internal(url)
    except HTTPException as exc:
        raise TeamsWebhookRefused(str(exc.detail)) from None


async def _find(db: AsyncSession, user_id: int) -> Optional[Integration]:
    result = await db.execute(
        select(Integration).where(
            Integration.user_id == user_id,
            Integration.provider == TEAMS_WEBHOOK_PROVIDER,
        )
    )
    return result.scalar_one_or_none()


async def save_teams_webhook(db: AsyncSession, user_id: int, url: str) -> None:
    """Valide puis enregistre (ou remplace) le webhook Teams de ``user_id``.

    Lève ``TeamsWebhookRefused`` si l'URL n'est pas un webhook Teams/Power
    Automate plausible ; rien n'est écrit dans ce cas.
    """
    checked = validate_teams_webhook_url(url)
    encrypted = encrypt_value(checked)

    integration = await _find(db, user_id)
    if integration is not None:
        integration.access_token = encrypted  # type: ignore[assignment]
    else:
        integration = Integration(
            user_id=user_id,
            provider=TEAMS_WEBHOOK_PROVIDER,
            access_token=encrypted,
        )
        db.add(integration)
    await db.commit()


async def delete_teams_webhook(db: AsyncSession, user_id: int) -> bool:
    """Supprime le webhook Teams de ``user_id`` ; ``True`` s'il existait."""
    integration = await _find(db, user_id)
    if integration is None:
        return False
    await db.delete(integration)
    await db.commit()
    return True


async def is_teams_webhook_configured(db: AsyncSession, user_id: int) -> bool:
    return await _find(db, user_id) is not None


async def get_teams_webhook_url_for_owner(owner_email: str) -> Optional[str]:
    """URL déchiffrée du webhook Teams du propriétaire d'un run de workflow,
    résolu par email (comme le reste de ``workflow_runtime``), ou ``None``
    si l'intégration n'est pas configurée. Ouvre sa propre session, même
    schéma que ``workflow_runtime._notify_owner_in_app``."""
    from apowerb.helpers.database import sessionmanager
    from apowerb.users.service import get_user_by_email

    async with sessionmanager.session() as db:
        user = await get_user_by_email(owner_email, db)
        result = await db.execute(
            select(Integration).where(
                Integration.user_id == user.user_id,
                Integration.provider == TEAMS_WEBHOOK_PROVIDER,
            )
        )
        integration = result.scalar_one_or_none()
        if integration is None or not integration.access_token:
            return None
        return decrypt_value(integration.access_token)

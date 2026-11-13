"""Abonnement Microsoft Graph propre aux workflows (trigger ``email`` Outlook).

Un workflow publié dont le trigger est ``email``/``outlook`` doit entendre la
boîte de son propriétaire sans qu'un agent y soit abonné. Ce module tient,
pour chaque propriétaire qui a au moins un tel trigger armé, UNE ligne
``webhook_subscriptions`` avec ``consumer = 'workflow'`` (et ``agent_id``
NULL, imposé par la contrainte CHECK) — l'abonnement workflow :

* une notification reçue sur cet abonnement ne lance aucun agent, seulement
  les triggers ``email`` du propriétaire (``routers/webhook_handlers/outlook.py``) ;
* un seul abonnement par propriétaire et fournisseur (index unique partiel) :
  deux abonnements sur la même boîte livreraient chaque mail deux fois ;
* les abonnements agents (``consumer = 'agent'``) ne sont jamais lus
  ni modifiés ici.

``reconcile_outlook_subscriptions`` est appelée à chaque tour de
``flow_scheduler`` : elle crée l'abonnement qui manque, recrée celui que le
renouvellement a laissé ``expired`` ou ``error`` (après ``RETRY_AFTER``), et
retire celui dont plus aucun trigger n'a besoin. Le renouvellement lui-même
reste celui de tous les abonnements (``scheduler/webhook_renewal.py``).

Plusieurs workers peuvent réconcilier en même temps : la ligne est réservée
(``status='pending'``) AVANT l'appel à Graph, par un INSERT que l'index unique
départage ou par un UPDATE conditionnel ; seul le gagnant appelle Graph.

Gmail : pas d'abonnement workflow en v1 (le chemin agent reste le seul).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from logging import getLogger
from typing import Any, Optional

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from apowerb.configs.settings import get_settings
from apowerb.core import workflow_triggers as wt
from apowerb.helpers.database import sessionmanager
from apowerb.integrations.outlook_webhook import OutlookWebhookService
from apowerb.models import (
    WEBHOOK_CONSUMER_WORKFLOW,
    Integration,
    User,
    WebhookSubscription,
)

logger = getLogger(__name__)

PROVIDER = "microsoft_outlook"
RESOURCE = "me/mailFolders('Inbox')/messages"
CHANGE_TYPE = "created"

# Délai avant de retenter un abonnement en échec (ou une réservation restée
# ``pending`` après un crash) : la réconciliation tourne toutes les 30 s, Graph
# ne doit pas recevoir une création ratée à ce rythme.
RETRY_AFTER = timedelta(minutes=10)


def _notification_url() -> str:
    return f"{get_settings().public_base_url}/api/webhooks/outlook/notifications"


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    """SQLite rend des dates naïves : elles sont en UTC."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _wanted_owner_emails() -> set[str]:
    owners: set[str] = set()
    for row in wt.list_active_triggers("email"):
        try:
            cfg = json.loads(row.get("config") or "{}")
        except (TypeError, ValueError):
            continue
        if cfg.get("provider") == "outlook":
            owners.add(row["owner_id"])
    return owners


def _workflow_sub_filter():
    return (
        WebhookSubscription.consumer == WEBHOOK_CONSUMER_WORKFLOW,
        WebhookSubscription.provider == PROVIDER,
    )


async def has_active_workflow_subscription(
    db, user_id: int, *, received_at: Optional[datetime] = None
) -> bool:
    """``True`` si un abonnement workflow Outlook actif de ``user_id`` couvre
    un mail reçu à ``received_at`` (abonnement créé avant lui) — le chemin
    agent ne lance alors pas les workflows lui-même. Un mail plus ancien n'a
    été notifié qu'à l'agent (backlog lent) : c'est encore à lui de le faire.
    Sans ``received_at`` : l'abonnement actif suffit."""
    row = (
        await db.execute(
            select(WebhookSubscription.created_at).where(
                *_workflow_sub_filter(),
                WebhookSubscription.user_id == user_id,
                WebhookSubscription.status == "active",
            )
        )
    ).first()
    if row is None:
        return False
    created = _aware(row[0])
    if received_at is None or created is None:
        return True
    return created <= _aware(received_at)


async def _owners_with_integration(db, emails: set[str]) -> set[int]:
    if not emails:
        return set()
    rows = await db.execute(
        select(User.user_id)
        .join(Integration, Integration.user_id == User.user_id)
        .where(User.email.in_(emails), Integration.provider == PROVIDER)
        .distinct()
    )
    return {r[0] for r in rows}


async def _graph_delete(user_id: int, graph_id: Optional[str]) -> None:
    """Best-effort : un abonnement Graph oublié expire de lui-même en 3 jours,
    et ses notifications sont ignorées (``subscription_id`` inconnu)."""
    if not graph_id:
        return
    try:
        async with sessionmanager.session() as db:
            token = await OutlookWebhookService.get_access_token_for_user(db, user_id)
        await OutlookWebhookService.delete_subscription(token, graph_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[WF MAIL SUB] suppression Graph de %s (user_id=%s) en échec : %s",
            graph_id, user_id, exc,
        )


async def _claim(user_id: int, existing: Any, client_state: str, now: datetime) -> bool:
    """Réserve la ligne de ``user_id`` pour CE processus. ``False`` : un autre
    worker l'a déjà (ou vient de la prendre)."""
    async with sessionmanager.session() as db:
        if existing is None:
            db.add(
                WebhookSubscription(
                    user_id=user_id,
                    provider=PROVIDER,
                    resource=RESOURCE,
                    change_type=CHANGE_TYPE,
                    notification_url=_notification_url(),
                    client_state=client_state,
                    consumer=WEBHOOK_CONSUMER_WORKFLOW,
                    agent_id=None,
                    status="pending",
                    updated_at=now,
                )
            )
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                return False
            return True
        result = await db.execute(
            update(WebhookSubscription)
            .where(
                WebhookSubscription.id == existing.id,
                # client_state change à chaque réservation : jeton de
                # concurrence (une égalité de dates ne survit pas à SQLite).
                WebhookSubscription.status == existing.status,
                WebhookSubscription.client_state == existing.client_state,
            )
            .values(status="pending", client_state=client_state, updated_at=now)
        )
        await db.commit()
        return result.rowcount == 1


async def _create(user_id: int, existing: Any, now: datetime) -> Optional[bool]:
    """``True`` créé, ``False`` échec consigné, ``None`` pris par un autre worker."""
    client_state = OutlookWebhookService.generate_client_state()
    if not await _claim(user_id, existing, client_state, now):
        return None
    if existing is not None:
        await _graph_delete(user_id, existing.subscription_id)
    try:
        async with sessionmanager.session() as db:
            token = await OutlookWebhookService.get_access_token_for_user(db, user_id)
        graph_sub = await OutlookWebhookService.create_subscription(
            access_token=token,
            notification_url=_notification_url(),
            resource=RESOURCE,
            change_type=CHANGE_TYPE,
            client_state=client_state,
        )
        expiry = None
        raw = graph_sub.get("expirationDateTime")
        if raw:
            try:
                expiry = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except (TypeError, ValueError):
                expiry = None
        values = dict(
            subscription_id=graph_sub.get("id"),
            expiration_datetime=expiry,
            notification_url=_notification_url(),
            status="active",
        )
    except Exception as exc:  # noqa: BLE001 -- consigné sur la ligne, retenté plus tard
        logger.warning(
            "[WF MAIL SUB] création de l'abonnement workflow de user_id=%s en échec : %s",
            user_id, exc,
        )
        values = dict(subscription_id=None, status="error", updated_at=now)
    async with sessionmanager.session() as db:
        # Clé = la réservation (client_state) : une ligne retirée ou reprise
        # entre-temps n'est pas écrasée.
        result = await db.execute(
            update(WebhookSubscription)
            .where(
                *_workflow_sub_filter(),
                WebhookSubscription.user_id == user_id,
                WebhookSubscription.client_state == client_state,
            )
            .values(**values)
        )
        await db.commit()
    if result.rowcount != 1 and values.get("subscription_id"):
        # Ligne disparue pendant l'appel : ne pas laisser l'abonnement Graph orphelin.
        await _graph_delete(user_id, values["subscription_id"])
        return None
    return values["status"] == "active"


async def reconcile_outlook_subscriptions(*, now: Optional[datetime] = None) -> dict:
    """Aligne les abonnements workflow Outlook sur les triggers armés.

    Renvoie ``{"created", "removed", "failed"}`` (comptes de ce passage).
    """
    now = now or datetime.now(timezone.utc)
    counts = {"created": 0, "removed": 0, "failed": 0}
    wanted_emails = _wanted_owner_emails()
    async with sessionmanager.session() as db:
        wanted = await _owners_with_integration(db, wanted_emails)
        subs = (
            await db.execute(
                select(
                    WebhookSubscription.id,
                    WebhookSubscription.user_id,
                    WebhookSubscription.status,
                    WebhookSubscription.subscription_id,
                    WebhookSubscription.client_state,
                    WebhookSubscription.updated_at,
                ).where(*_workflow_sub_filter())
            )
        ).all()

    by_user = {s.user_id: s for s in subs}
    for s in subs:
        if s.user_id in wanted:
            continue
        async with sessionmanager.session() as db:
            await db.execute(delete(WebhookSubscription).where(WebhookSubscription.id == s.id))
            await db.commit()
        await _graph_delete(s.user_id, s.subscription_id)
        counts["removed"] += 1

    for user_id in sorted(wanted):
        existing = by_user.get(user_id)
        if existing is not None:
            if existing.status == "active":
                continue
            last = _aware(existing.updated_at)
            if last is not None and now - last < RETRY_AFTER:
                continue
        outcome = await _create(user_id, existing, now)
        if outcome is True:
            counts["created"] += 1
        elif outcome is False:
            counts["failed"] += 1
    if any(counts.values()):
        logger.info("[WF MAIL SUB] réconciliation : %s", counts)
    return counts

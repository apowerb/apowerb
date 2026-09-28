"""Notification de rupture de prevision, dedoublonnee (contrat etape 7
SS2c). Separe de `forecast_tracking.py` (pur) : ce module fait l'I/O
(SQLAlchemy async). La route (`routers/forecast.py`) le compose.
"""

from __future__ import annotations

import json
import logging
from datetime import date as date_type
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from apowerb.bi.db_stores import DatabaseDashboardStore
from apowerb.helpers.notification_bus import notify as push_notification
from apowerb.models import BIForecastAlert, Notification, User

logger = logging.getLogger(__name__)


class ForecastAlertStore:
    """Reservation idempotente d'une alerte (chart_id, group, level, date).

    `group`/`level` sont normalises en chaine vide (jamais NULL) : deux NULL
    ne sont jamais egaux dans une UniqueConstraint, donc les laisser tels
    quels romprait le dedoublonnage pour toute serie sans hierarchie.
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def try_reserve(
        self, *, chart_id: str, group: Any, level: Any, date: date_type, commit: bool = True
    ) -> bool:
        """Insere la reservation si elle n'existe pas deja. Renvoie True si
        c'est une nouvelle alerte (il faut notifier), False si elle a deja
        ete vue (dedoublonnage). Le conflit est isole dans un point de
        sauvegarde : il n'annule pas le reste de la transaction. Avec
        `commit=False`, la reservation attend le commit de l'appelant (elle
        est alors validee ou annulee avec la notification)."""
        row = BIForecastAlert(
            chart_id=chart_id,
            group_key=str(group) if group is not None else "",
            level_key=str(level) if level is not None else "",
            date=date,
        )
        try:
            async with self._db.begin_nested():
                self._db.add(row)
        except IntegrityError:
            return False
        if commit:
            await self._db.commit()
        return True


async def dashboard_link_for_chart(db: AsyncSession, *, owner: str, chart_id: str) -> str:
    """Lien de l'UI (`/bi/<dashboardId>`) vers le tableau de bord le plus
    recent du proprietaire qui affiche ce graphique ; `/bi` si aucun ne le
    contient (graphique retire depuis, ou plus de 100 tableaux de bord)."""
    dashboards, _ = await DatabaseDashboardStore(db, owner=owner).list(page_size=100)
    for dashboard in dashboards:
        if any(c.chart is not None and c.chart.chart_id == chart_id for c in dashboard.components):
            return f"/bi/{dashboard.id}"
    return "/bi"


async def _resolve_user_id(db: AsyncSession, owner_email: str) -> int | None:
    """Meme regle d'egalite que `_BaseBIStore` (bi/db_stores.py) : owner
    insensible a la casse."""
    q = select(User.user_id).where(func.lower(User.email) == owner_email.lower())
    return (await db.execute(q)).scalars().first()


async def notify_breach(
    db: AsyncSession,
    *,
    chart_id: str,
    owner_email: str,
    group: Any,
    level: Any,
    date: date_type,
    direction: str,
    kind: str,
    link: str,
) -> bool:
    """Reserve puis notifie le proprietaire du graphique si c'est une
    nouvelle alerte (contrat etape 7 SS2c). Ne notifie jamais deux fois la
    meme (chart_id, group, level, date). Renvoie True si une notification a
    ete creee.
    """
    # Proprietaire resolu AVANT la reservation : sinon une alerte sans
    # destinataire serait marquee vue et jamais renvoyee.
    user_id = await _resolve_user_id(db, owner_email)
    if user_id is None:
        logger.warning("forecast alert: no user found for owner %s, notification skipped", owner_email)
        return False

    # Reservation et notification validees par le MEME commit : un echec
    # entre les deux annule la reservation, l'alerte repartira au prochain
    # calcul au lieu d'etre perdue.
    store = ForecastAlertStore(db)
    if not await store.try_reserve(chart_id=chart_id, group=group, level=level, date=date, commit=False):
        return False

    direction_fr = "au-dessus" if direction == "above" else "en-dessous"
    series_label = str(group) if group else "la serie"
    title = "Rupture de prevision"
    message = f"{series_label} est {direction_fr} de la bande prevue le {date.isoformat()}."

    notification = Notification(
        user_id=user_id,
        title=title,
        message=message,
        type="warning",
        link=link,
        metadata_json=json.dumps(
            {
                "chart_id": chart_id,
                "group": group,
                "level": level,
                "date": date.isoformat(),
                "direction": direction,
                "kind": kind,
            }
        ),
        is_read=False,
    )
    db.add(notification)
    await db.commit()
    await db.refresh(notification)

    await push_notification(
        user_id,
        {
            "id": notification.id,
            "title": notification.title,
            "message": notification.message,
            "type": notification.type,
            "link": notification.link,
            "is_read": False,
            "created_at": notification.created_at.isoformat() if notification.created_at else None,
        },
    )
    return True

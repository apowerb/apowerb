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
        self, *, chart_id: str, group: Any, level: Any, date: date_type
    ) -> bool:
        """Insere la reservation si elle n'existe pas deja. Renvoie True si
        c'est une nouvelle alerte (il faut notifier), False si elle a deja
        ete vue (dedoublonnage)."""
        row = BIForecastAlert(
            chart_id=chart_id,
            group_key=str(group) if group is not None else "",
            level_key=str(level) if level is not None else "",
            date=date,
        )
        self._db.add(row)
        try:
            await self._db.commit()
            return True
        except IntegrityError:
            await self._db.rollback()
            return False


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
    store = ForecastAlertStore(db)
    is_new = await store.try_reserve(chart_id=chart_id, group=group, level=level, date=date)
    if not is_new:
        return False

    user_id = await _resolve_user_id(db, owner_email)
    if user_id is None:
        logger.warning("forecast alert: no user found for owner %s, notification skipped", owner_email)
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

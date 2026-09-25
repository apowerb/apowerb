"""Persistance des instantanés de prévision (``bi_forecast_snapshots``).

Séparé de ``forecast_tracking.py`` : ce module fait l'I/O (SQLAlchemy async),
``forecast_tracking`` reste pur et testable sans base. La route
(``routers/forecast.py``) les compose.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from apowerb.models import BIForecastSnapshot

logger = logging.getLogger(__name__)

# Contrat étape 5 §3 : au plus 60 instantanés conservés par graphique.
RETENTION_PER_CHART = 60


class ForecastSnapshotStore:
    """CRUD minimal sur ``bi_forecast_snapshots``, scopé à un graphique."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def list_for_chart(self, chart_id: str) -> list[BIForecastSnapshot]:
        q = select(BIForecastSnapshot).where(BIForecastSnapshot.chart_id == chart_id)
        return list((await self._db.execute(q)).scalars().all())

    async def upsert(
        self,
        *,
        chart_id: str,
        owner: str,
        organization_id: str,
        config_hash: str,
        history_end: date,
        frequency: str | None,
        payload: dict[str, Any],
    ) -> None:
        """Insère l'instantané, ou le remplace s'il existe déjà pour
        (chart_id, config_hash, history_end) — pas de doublon à chaque
        affichage. Applique ensuite la rétention à 60 par graphique."""
        q = select(BIForecastSnapshot).where(
            BIForecastSnapshot.chart_id == chart_id,
            BIForecastSnapshot.config_hash == config_hash,
            BIForecastSnapshot.history_end == history_end,
        )
        row = (await self._db.execute(q)).scalars().first()
        if row is not None:
            row.payload = payload
            row.frequency = frequency
            row.owner = owner
            row.organization_id = organization_id
        else:
            row = BIForecastSnapshot(
                chart_id=chart_id,
                owner=owner,
                organization_id=organization_id,
                config_hash=config_hash,
                history_end=history_end,
                frequency=frequency,
                payload=payload,
            )
            self._db.add(row)
        await self._db.commit()
        await self._enforce_retention(chart_id)

    async def _enforce_retention(self, chart_id: str, *, keep: int = RETENTION_PER_CHART) -> None:
        # Le plus ancien = celui dont l'historique s'arrête le plus tôt
        # (`history_end`), pas celui écrit en premier : deux instantanés
        # peuvent être upsertés dans la même seconde (`created_at` ne
        # départage pas de façon fiable sous SQLite), alors que `history_end`
        # reflète l'ordre réel des prévisions.
        q = (
            select(BIForecastSnapshot.id)
            .where(BIForecastSnapshot.chart_id == chart_id)
            .order_by(BIForecastSnapshot.history_end.desc(), BIForecastSnapshot.created_at.desc())
            .offset(keep)
        )
        stale_ids = [row_id for (row_id,) in (await self._db.execute(q)).all()]
        if stale_ids:
            await self._db.execute(delete(BIForecastSnapshot).where(BIForecastSnapshot.id.in_(stale_ids)))
            await self._db.commit()

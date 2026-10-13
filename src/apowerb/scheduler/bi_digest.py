"""Boucle de tick des resumes et alertes de tableaux de bord BI.

Meme principe que `core/flow_scheduler.py` : un tick local sur une table
locale, pas d'orchestrateur externe. Le verrou entre repliques est porte par la
ligne (voir `bi/dashboard_digest.tick_once`).
"""

from __future__ import annotations

import asyncio
from logging import getLogger

from apowerb.bi.dashboard_digest import tick_once
from apowerb.helpers.database import sessionmanager

logger = getLogger(__name__)

TICK_SECONDS = 300
_task: asyncio.Task | None = None


async def _loop() -> None:
    logger.info("[bi_digest] boucle de tick démarrée (%ss)", TICK_SECONDS)
    while True:
        try:
            async with sessionmanager.session() as db:
                await tick_once(db)
        except Exception:  # noqa: BLE001 - la boucle doit survivre à un tick en échec
            logger.exception("[bi_digest] tick en échec")
        await asyncio.sleep(TICK_SECONDS)


def start() -> None:
    """Démarre la boucle une fois par processus (idempotent)."""
    global _task
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(_loop())


def stop() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        _task = None

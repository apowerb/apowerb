"""Daily purge of memory entries past ``MEMORY_RETENTION_DAYS`` (default 90).

Search already ignores expired entries; this loop is what actually deletes
them. It never crashes the app: a failed pass is logged and retried next day.
"""

from __future__ import annotations

import asyncio
from logging import getLogger

from apowerb.memory.service import PersistentMemoryService

logger = getLogger(__name__)

CHECK_INTERVAL_SECONDS = 24 * 3600


async def memory_retention_loop(service: PersistentMemoryService | None = None) -> None:
    if service is None:
        from apowerb.configs.settings import get_settings

        service = PersistentMemoryService(schema=get_settings().db_schema or None)
    while True:
        try:
            deleted = await service.purge_expired()
            if deleted:
                logger.info("[MEMORY RETENTION] purged %d expired memory entries", deleted)
        except Exception as exc:  # noqa: BLE001 -- background task must survive
            logger.warning("[MEMORY RETENTION] purge pass failed: %r", exc)
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)

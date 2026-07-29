"""Background task that purges old ADK session events.

ADK's ``DatabaseSessionService`` has NO retention policy: the ``events``
table (one row per agent turn, ``event_data`` = the full model_dump of
the event) grows without bound. Left alone it silently eats the shared
``defaultdb`` instance. This loop deletes events older than
``ADK_EVENTS_RETENTION_DAYS`` (default 90) once a day.

Scope: ``events`` only — the large jsonb rows. Sessions and state tables
are left intact (a purged session keeps its shell, just loses old turn
detail). The ADK events table lives in the application schema
(``settings.db_schema``) because the session engine pins ``search_path``
there (see ``main.py``); the DELETE is schema-qualified accordingly.

The pass is defensive: any failure is logged and swallowed so the
background task never crashes the app.
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timedelta, timezone
from logging import getLogger

from sqlalchemy import text

from th2agent.configs.settings import get_settings
from th2agent.helpers.database import sessionmanager

logger = getLogger(__name__)

# Run once a day — retention is coarse (days), no need to poll often.
CHECK_INTERVAL_SECONDS = 24 * 3600

_DEFAULT_RETENTION_DAYS = 90
# db_schema is interpolated as a SQL identifier (cannot be a bind param),
# so it must be a plain identifier. It comes from our own env, but we
# validate anyway to keep the DELETE injection-proof.
_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _retention_days() -> int:
    """Retention window in days, from ``ADK_EVENTS_RETENTION_DAYS`` (default 90)."""
    raw = os.environ.get("ADK_EVENTS_RETENTION_DAYS")
    if raw is None:
        return _DEFAULT_RETENTION_DAYS
    try:
        days = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "[EVENTS RETENTION] invalid ADK_EVENTS_RETENTION_DAYS=%r, using %d",
            raw, _DEFAULT_RETENTION_DAYS,
        )
        return _DEFAULT_RETENTION_DAYS
    if days < 1:
        logger.warning(
            "[EVENTS RETENTION] ADK_EVENTS_RETENTION_DAYS=%d < 1, using %d",
            days, _DEFAULT_RETENTION_DAYS,
        )
        return _DEFAULT_RETENTION_DAYS
    return days


def _events_table() -> str:
    """Schema-qualified ``events`` table name, validated as a safe identifier."""
    schema = get_settings().db_schema
    if not _SCHEMA_RE.match(schema or ""):
        raise ValueError(f"unsafe db_schema for events purge: {schema!r}")
    return f"{schema}.events"


async def _purge_old_events() -> int:
    """Single purge pass. Deletes events older than the retention window.

    Returns the number of rows deleted.
    """
    days = _retention_days()
    # ADK stores ``timestamp`` as ``timestamp without time zone`` in UTC,
    # so compare against a naive-UTC cutoff.
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    table = _events_table()
    async with sessionmanager.session() as db:
        # ADK's ``events`` table has only a composite PK and no index on
        # ``timestamp``; without one the daily DELETE seq-scans the table and
        # competes for I/O on the SHARED defaultdb instance (cf 2026-05-22
        # saturation). Create it once (idempotent) so the purge stays cheap.
        await db.execute(
            text(
                f'CREATE INDEX IF NOT EXISTS ix_adk_events_timestamp '
                f'ON {table} ("timestamp")'
            )
        )
        result = await db.execute(
            text(f'DELETE FROM {table} WHERE "timestamp" < :cutoff'),
            {"cutoff": cutoff},
        )
        await db.commit()
        deleted = result.rowcount or 0
    if deleted:
        logger.info(
            "[EVENTS RETENTION] purged %d events older than %d days", deleted, days
        )
    return deleted


async def events_retention_loop() -> None:
    """Daily loop: purge old ADK events, then sleep. Never crashes the app."""
    while True:
        try:
            await _purge_old_events()
        except Exception as exc:  # noqa: BLE001 - background task must survive
            logger.warning("[EVENTS RETENTION] purge pass failed: %r", exc)
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)

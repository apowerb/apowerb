"""Self-healing schema migration for the ``webhook_logs`` queue.

PR #120 widened the ``webhook_logs`` model with the queue/backlog
columns (``resource_id``, ``payload_json``, ``attempts``,
``next_attempt_at``, ``started_at``, ``completed_at``) and two new
indexes. The runtime relies on
``Base.metadata.create_all(checkfirst=True)`` which only creates
*missing tables* — it never alters an existing one.

On SCEI_PROD this meant the table stayed on the old shape after the
PR #120 deploy and every Microsoft Graph notification raised
``UndefinedColumnError``. The fix was a one-shot migration script,
but the same trap is waiting on every other environment that
predates PR #120 (OVH_DEV, DAVE_OVH, anyone redeploying from a stale
DB).

This module ports that migration into the worker's startup path so
the schema heals itself the first time the worker boots in a fresh
environment — and stays a no-op on every subsequent boot. The
operations are guarded by ``ADD COLUMN IF NOT EXISTS`` and
``CREATE INDEX IF NOT EXISTS`` so concurrent boots cannot collide.

We deliberately keep the migration narrow:
- only DDL on the table the worker owns (``webhook_logs``)
- only additive changes (no drop / rename)
- failures are logged but never block the worker — the operator can
  still rerun the standalone script if needed.
"""
from __future__ import annotations

from typing import Iterable

from sqlalchemy import text

from apowerb.configs.settings import get_settings
from apowerb.configs.th2logger import setup_logging
from apowerb.helpers.database import sessionmanager


logger = setup_logging(__name__)


def _ddl_statements(schema: str) -> Iterable[tuple[str, str]]:
    """The DDL we want applied, in order. Each entry is
    ``(label, statement)`` — the label appears in INFO logs so the
    operator can see exactly which step is being run."""
    qualified = f"{schema}.webhook_logs" if schema else "webhook_logs"
    return (
        (
            "resource_id column",
            f"ALTER TABLE {qualified} "
            "ADD COLUMN IF NOT EXISTS resource_id VARCHAR(500);",
        ),
        (
            "payload_json column",
            f"ALTER TABLE {qualified} "
            "ADD COLUMN IF NOT EXISTS payload_json TEXT;",
        ),
        (
            "attempts column",
            f"ALTER TABLE {qualified} "
            "ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0;",
        ),
        (
            "next_attempt_at column",
            f"ALTER TABLE {qualified} "
            "ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMP WITH TIME ZONE;",
        ),
        (
            "started_at column",
            f"ALTER TABLE {qualified} "
            "ADD COLUMN IF NOT EXISTS started_at TIMESTAMP WITH TIME ZONE;",
        ),
        (
            "completed_at column",
            f"ALTER TABLE {qualified} "
            "ADD COLUMN IF NOT EXISTS completed_at TIMESTAMP WITH TIME ZONE;",
        ),
        (
            "force_reprocess column",
            f"ALTER TABLE {qualified} "
            "ADD COLUMN IF NOT EXISTS force_reprocess BOOLEAN NOT NULL DEFAULT FALSE;",
        ),
        (
            "ix_webhook_logs_pick index",
            f"CREATE INDEX IF NOT EXISTS ix_webhook_logs_pick "
            f"ON {qualified} (status, next_attempt_at, id);",
        ),
        (
            "ux_webhook_logs_sub_resource unique index",
            f"CREATE UNIQUE INDEX IF NOT EXISTS ux_webhook_logs_sub_resource "
            f"ON {qualified} (subscription_id, resource_id) "
            f"WHERE resource_id IS NOT NULL;",
        ),
    )


async def ensure_webhook_logs_schema() -> bool:
    """Apply the queue/backlog DDL to ``webhook_logs``.

    Returns ``True`` when every statement ran (or was already applied),
    ``False`` when something went wrong but the worker can keep going
    with whatever schema is currently in place.

    Uses ``IF NOT EXISTS`` everywhere so the function is safe to call
    on every worker boot. SQLite does not support ``ADD COLUMN IF NOT
    EXISTS`` — when the dialect detects a SQLite bind we simply skip
    the migration (the test fixture builds the full schema via
    ``Base.metadata.create_all`` so the columns are already there).
    """
    settings = get_settings()
    schema = settings.db_schema or ""

    try:
        async with sessionmanager.session() as db:
            dialect_name = db.bind.dialect.name if db.bind else ""
            if dialect_name == "sqlite":
                logger.info(
                    "[BACKLOG] schema migration skipped on sqlite "
                    "(create_all already covers the columns)"
                )
                return True

            for label, statement in _ddl_statements(schema):
                logger.info("[BACKLOG] applying %s", label)
                await db.execute(text(statement))
            await db.commit()
            logger.info(
                "[BACKLOG] webhook_logs schema ensured (schema=%r, dialect=%s)",
                schema, dialect_name,
            )
            return True
    except Exception:
        # We never want a DDL hiccup to crash the worker — the
        # standalone scripts/add_webhook_logs_backlog_columns.py is
        # the operator's escape hatch.
        logger.exception(
            "[BACKLOG] webhook_logs schema migration failed — "
            "worker will continue but rows may not insert. "
            "Run scripts/add_webhook_logs_backlog_columns.py to recover."
        )
        return False

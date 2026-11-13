"""Self-healing schema migration for the ``webhook_logs`` queue.

PR #120 widened the ``webhook_logs`` model with the queue/backlog
columns (``resource_id``, ``payload_json``, ``attempts``,
``next_attempt_at``, ``started_at``, ``completed_at``) and two new
indexes. The runtime relies on
``Base.metadata.create_all(checkfirst=True)`` which only creates
*missing tables* — it never alters an existing one.

On that deployment this meant the table stayed on the old shape after the
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


def _history_fk_statements(schema: str) -> Iterable[tuple[str, str]]:
    """Make ``webhook_logs.subscription_id`` outlive its subscription.

    Older tables were created ``NOT NULL`` + ``ON DELETE CASCADE``: deleting
    one subscription row deleted its whole execution history (incident
    2026-11-09, 11 179 logs restored from a backup). Converge on a nullable
    column whose foreign key is ``ON DELETE SET NULL``. Idempotent: a foreign
    key already in ``SET NULL`` is left untouched.
    """
    qualified = f"{schema}.webhook_logs" if schema else "webhook_logs"
    parent = f"{schema}.webhook_subscriptions" if schema else "webhook_subscriptions"
    nsp = f"'{schema}'" if schema else "current_schema()"
    fk_on_subscription = (
        "FROM pg_constraint con "
        "JOIN pg_class t ON t.oid = con.conrelid "
        "JOIN pg_namespace n ON n.oid = t.relnamespace "
        "JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY (con.conkey) "
        f"WHERE con.contype = 'f' AND n.nspname = {nsp} "
        "AND t.relname = 'webhook_logs' AND a.attname = 'subscription_id'"
    )
    return (
        (
            "subscription_id nullable",
            f"ALTER TABLE {qualified} ALTER COLUMN subscription_id DROP NOT NULL;",
        ),
        (
            "subscription_id foreign key ON DELETE SET NULL",
            "DO $fk$ DECLARE c record; BEGIN "
            f"FOR c IN SELECT con.conname {fk_on_subscription} AND con.confdeltype <> 'n' LOOP "
            f"EXECUTE format('ALTER TABLE {qualified} DROP CONSTRAINT %I', c.conname); "
            "END LOOP; "
            f"IF NOT EXISTS (SELECT 1 {fk_on_subscription} AND con.confdeltype = 'n') THEN "
            f"ALTER TABLE {qualified} ADD CONSTRAINT webhook_logs_subscription_id_fkey "
            f"FOREIGN KEY (subscription_id) REFERENCES {parent}(id) ON DELETE SET NULL; "
            "END IF; END $fk$;",
        ),
    )


async def _ensure_history_fk(schema: str) -> bool:
    """Run :func:`_history_fk_statements` in its own transaction, so a failure
    here never rolls back the queue columns applied before it."""
    try:
        async with sessionmanager.session() as db:
            for label, statement in _history_fk_statements(schema):
                logger.info("[BACKLOG] applying %s", label)
                await db.execute(text(statement))
            await db.commit()
        return True
    except Exception:
        logger.exception(
            "[BACKLOG] webhook_logs.subscription_id is still CASCADE or NOT NULL: "
            "deleting a subscription may delete its history."
        )
        return False


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
        return await _ensure_history_fk(schema)
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

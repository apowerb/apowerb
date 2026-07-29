#!/usr/bin/env python3
"""Add Fernet-prefix CheckConstraints to the ``integrations`` table.

Defense-in-depth migration for the 2026-05-07 SCEI plaintext-token incident.
After this script runs, any future INSERT/UPDATE that puts a non-Fernet
value into ``integrations.access_token`` or ``integrations.refresh_token``
will be rejected by the database (not just the ORM).

Steps:
1. Re-encrypt any legacy plaintext tokens in place (calls
   ``encrypt_legacy_integration_tokens`` from the helpers module).
2. Verify there is no remaining plaintext row that would fail the
   constraint we are about to add.
3. ``ALTER TABLE ... ADD CONSTRAINT`` for the two CHECKs. Idempotent —
   already-present constraints are skipped.

Usage:
    python scripts/add_integration_token_check_constraint.py --dry-run
    python scripts/add_integration_token_check_constraint.py

Run on every VM that shares the OVH PostgreSQL DB (SCEI_PROD, OVH_DEV,
DAVE_OVH) with the proper schema set in the env (``DB_SCHEMA``).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

# Ensure the th2agent package is importable when running from the repo root.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sqlalchemy import create_engine, text

from th2agent.configs.settings import get_settings
from th2agent.helpers.database_connection import DBConfig
from th2agent.integrations.helpers import encrypt_legacy_integration_tokens


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("add_integration_token_check_constraint")


SETTINGS = get_settings()
SCHEMA = SETTINGS.db_schema if SETTINGS.db_schema and SETTINGS.db_schema != "public" else None
TABLE = "integrations"

CONSTRAINTS = [
    (
        "ck_integrations_access_token_fernet",
        "access_token IS NULL OR access_token = '' OR access_token LIKE 'gAAAAA%'",
    ),
    (
        "ck_integrations_refresh_token_fernet",
        "refresh_token IS NULL OR refresh_token = '' OR refresh_token LIKE 'gAAAAA%'",
    ),
]


def _qualified(table: str) -> str:
    return f'"{SCHEMA}"."{table}"' if SCHEMA else f'"{table}"'


def _build_engine():
    async_url = DBConfig().get_db_url()
    sync_url = async_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://")
    return create_engine(sync_url, echo=False, future=True)


def _count_plaintext(engine) -> int:
    """Count rows whose access_token / refresh_token would fail the CHECK."""
    qual = _qualified(TABLE)
    sql = text(
        f"""
        SELECT COUNT(*) FROM {qual}
        WHERE
          (access_token  IS NOT NULL AND access_token  <> '' AND access_token  NOT LIKE 'gAAAAA%')
          OR
          (refresh_token IS NOT NULL AND refresh_token <> '' AND refresh_token NOT LIKE 'gAAAAA%')
        """
    )
    with engine.connect() as conn:
        return conn.execute(sql).scalar_one()


def _constraint_exists(engine, name: str) -> bool:
    if SCHEMA:
        sql = text(
            """
            SELECT 1
            FROM information_schema.table_constraints
            WHERE constraint_name = :name
              AND table_name = :table
              AND table_schema = :schema
            """
        )
        params = {"name": name, "table": TABLE, "schema": SCHEMA}
    else:
        sql = text(
            """
            SELECT 1
            FROM information_schema.table_constraints
            WHERE constraint_name = :name
              AND table_name = :table
            """
        )
        params = {"name": name, "table": TABLE}
    with engine.connect() as conn:
        return conn.execute(sql, params).first() is not None


def _add_constraint(engine, name: str, expr: str, dry_run: bool) -> None:
    if _constraint_exists(engine, name):
        logger.info("Constraint %s already present — skipping.", name)
        return
    qual = _qualified(TABLE)
    stmt = f'ALTER TABLE {qual} ADD CONSTRAINT "{name}" CHECK ({expr})'
    if dry_run:
        logger.info("[dry-run] would run: %s", stmt)
        return
    with engine.begin() as conn:
        conn.execute(text(stmt))
    logger.info("Added constraint %s.", name)


# Advisory lock id — any stable 64-bit integer. Postgres advisory locks
# are session-scoped: a lock taken on connection X is held only as long
# as that connection is open. Two concurrent runs of this script (e.g.
# SCEI_PROD and OVH_DEV firing the same minute) serialise on the *same*
# integer key.
_ADVISORY_LOCK_KEY = 7423011_7423011  # readable + 63-bit safe


def _acquire_lock(engine):
    """Take a session-level advisory lock and **return the connection**
    that holds it.

    Postgres releases the lock the moment its session ends, so the
    caller MUST keep the returned connection open for the duration of
    the work and close it (after explicit unlock) in a ``finally`` block.

    Returns the live ``Connection`` on success, ``None`` if another
    process is already holding the lock.
    """
    conn = engine.connect()
    try:
        got = conn.execute(
            text("SELECT pg_try_advisory_lock(:k)"), {"k": _ADVISORY_LOCK_KEY}
        ).scalar_one()
    except Exception:
        conn.close()
        raise
    if not got:
        conn.close()
        return None
    return conn


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen, but do not modify the DB.",
    )
    args = parser.parse_args()

    logger.info("Schema: %s", SCHEMA or "(default)")
    engine = _build_engine()

    # 3 VMs share this DB. Without serialisation, two concurrent runs of
    # this script could both observe the same N plaintext rows and both
    # call encrypt_legacy_integration_tokens() — the helper is idempotent
    # (it skips already-Fernet rows via _looks_encrypted), so the worst
    # case is wasted writes rather than corruption. We still want the
    # serialise so logs read cleanly and so a future non-idempotent step
    # (e.g. a destructive cleanup) cannot race.
    lock_conn = None
    if not args.dry_run:
        lock_conn = _acquire_lock(engine)
        if lock_conn is None:
            logger.error(
                "Another instance of this migration appears to be running "
                "(advisory lock %d already held). Exiting without changes. "
                "If you believe this is stale, restart the holding process "
                "or wait for it to finish.",
                _ADVISORY_LOCK_KEY,
            )
            return 3
        logger.info("Acquired advisory lock %d.", _ADVISORY_LOCK_KEY)

    try:
        legacy = _count_plaintext(engine)
        logger.info("Plaintext (or non-Fernet) token rows detected: %d", legacy)

        if legacy > 0:
            if args.dry_run:
                logger.info("[dry-run] would call encrypt_legacy_integration_tokens()")
            else:
                migrated = encrypt_legacy_integration_tokens()
                logger.info("Re-encrypted %d row(s) via helper.", migrated)
                remaining = _count_plaintext(engine)
                if remaining > 0:
                    logger.error(
                        "Still %d plaintext token row(s) after migration — aborting.",
                        remaining,
                    )
                    return 2

        for name, expr in CONSTRAINTS:
            _add_constraint(engine, name, expr, dry_run=args.dry_run)

        logger.info("Done.")
        return 0
    finally:
        if lock_conn is not None:
            # Explicit unlock + close on the SAME connection that took
            # the lock. Postgres also releases the lock when the
            # connection drops (e.g. on SIGKILL or TCP reset), so a
            # crashing script does not leave a stale lock behind.
            try:
                lock_conn.execute(
                    text("SELECT pg_advisory_unlock(:k)"),
                    {"k": _ADVISORY_LOCK_KEY},
                )
                logger.info("Released advisory lock %d.", _ADVISORY_LOCK_KEY)
            except Exception as exc:
                logger.warning(
                    "Could not release advisory lock %d (will release on disconnect): %s",
                    _ADVISORY_LOCK_KEY, exc,
                )
            finally:
                lock_conn.close()


if __name__ == "__main__":
    sys.exit(main())

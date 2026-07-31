"""Auto-migration for the `shared_conversations` table.

Adds the `owner_id` and `is_public` columns used by the share endpoint to
enforce ownership and distinguish private vs. public share links.

Safe to call multiple times: each ALTER TABLE is guarded by an existence
check so it is a no-op when the column is already present.
"""

from sqlalchemy import create_engine, inspect as sa_inspect, text

from apowerb.configs.settings import get_settings
from apowerb.configs.th2logger import setup_logging
from apowerb.helpers.database_connection import DBConfig

logger = setup_logging(__name__)


def ensure_shared_conversations_columns() -> None:
    """Add `owner_id` and `is_public` to `shared_conversations` if missing."""
    settings = get_settings()
    cfg = DBConfig()
    sync_url = (
        f"{cfg.db_type}://{cfg.db_user}:{cfg.db_password}"
        f"@{cfg.db_host}:{cfg.db_port}/{cfg.db_name}"
    )
    engine = create_engine(sync_url)
    schema = settings.db_schema
    table_name = "shared_conversations"

    try:
        inspector = sa_inspect(engine)
        if not inspector.has_table(table_name, schema=schema):
            logger.debug(
                "Table '%s.%s' does not exist, skipping share migration.",
                schema,
                table_name,
            )
            return

        existing = {
            col["name"]
            for col in inspector.get_columns(table_name, schema=schema)
        }
        schema_prefix = f'"{schema}".' if schema else ""

        new_cols: dict[str, str] = {
            "owner_id": "VARCHAR(255)",
            "is_public": "BOOLEAN NOT NULL DEFAULT FALSE",
        }

        added: list[str] = []
        with engine.begin() as conn:
            for col, typ in new_cols.items():
                if col not in existing:
                    sql = (
                        f'ALTER TABLE {schema_prefix}"{table_name}" '
                        f'ADD COLUMN IF NOT EXISTS "{col}" {typ}'
                    )
                    logger.info("[share_migration] Running: %s", sql)
                    conn.execute(text(sql))
                    added.append(col)

            if "owner_id" in existing or "owner_id" in added:
                conn.execute(
                    text(
                        f'CREATE INDEX IF NOT EXISTS '
                        f'ix_shared_conversations_owner_id '
                        f'ON {schema_prefix}"{table_name}" ("owner_id")'
                    )
                )

            if "is_public" in added:
                backfill = conn.execute(
                    text(
                        f'UPDATE {schema_prefix}"{table_name}" '
                        f'SET is_public = TRUE WHERE owner_id IS NULL'
                    )
                )
                logger.info(
                    "[share_migration] Backfilled %d legacy shares to is_public=TRUE",
                    backfill.rowcount if backfill.rowcount is not None else 0,
                )

        if added:
            logger.info(
                "[share_migration] Added columns to '%s': %s", table_name, added
            )
        else:
            logger.debug(
                "[share_migration] Table '%s' is up to date.", table_name
            )
    except Exception as exc:
        logger.error(
            "Failed to ensure columns on '%s': %s", table_name, exc, exc_info=True
        )
    finally:
        engine.dispose()

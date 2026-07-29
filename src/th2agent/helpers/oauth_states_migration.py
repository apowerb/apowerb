"""Auto-migration for the `oauth_states` table (C4 OAuth CSRF protection)."""

from sqlalchemy import create_engine, inspect, text

from th2agent.configs.settings import get_settings
from th2agent.configs.th2logger import setup_logging
from th2agent.helpers.database_connection import DBConfig

logger = setup_logging(__name__)


def ensure_oauth_states_table() -> None:
    """Create the ``oauth_states`` table if missing.

    Safe to call multiple times — no-op when the table already exists.
    """
    settings = get_settings()
    try:
        async_url = DBConfig().get_db_url()
        sync_url = async_url.replace("postgresql+asyncpg://", "postgresql://")
        engine = create_engine(sync_url, echo=False)

        with engine.connect() as conn:
            inspector = inspect(engine)
            existing = inspector.get_table_names(schema=settings.db_schema)

            if "oauth_states" not in existing:
                logger.info("[oauth_states_migration] Creating table…")
                conn.execute(
                    text(
                        f"""
                        CREATE TABLE IF NOT EXISTS {settings.db_schema}.oauth_states (
                            state       VARCHAR(255) PRIMARY KEY,
                            user_id     INTEGER NOT NULL
                                           REFERENCES {settings.db_schema}."user"(user_id)
                                           ON DELETE CASCADE,
                            provider    VARCHAR(100) NOT NULL,
                            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            expires_at  TIMESTAMPTZ NOT NULL
                        );
                        """
                    )
                )
                conn.execute(
                    text(
                        f"CREATE INDEX IF NOT EXISTS "
                        f"ix_oauth_states_user_id "
                        f"ON {settings.db_schema}.oauth_states (user_id);"
                    )
                )
                conn.execute(
                    text(
                        f"CREATE INDEX IF NOT EXISTS "
                        f"ix_oauth_states_expires_at "
                        f"ON {settings.db_schema}.oauth_states (expires_at);"
                    )
                )
                conn.execute(
                    text(
                        f"CREATE INDEX IF NOT EXISTS "
                        f"ix_oauth_states_user_provider "
                        f"ON {settings.db_schema}.oauth_states (user_id, provider);"
                    )
                )
                conn.commit()
                logger.info("[oauth_states_migration] Table created.")
            else:
                logger.debug(
                    "[oauth_states_migration] Table already exists — skipping."
                )

        engine.dispose()

    except Exception as exc:
        logger.error(
            "[oauth_states_migration] Failed to ensure table: %s", exc, exc_info=True
        )

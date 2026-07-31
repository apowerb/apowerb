from apowerb.configs.th2logger import setup_logging
from sqlalchemy import create_engine, inspect, text

from apowerb.helpers.database_connection import DBConfig
from apowerb.configs.settings import get_settings

logger = setup_logging(__name__)
settings = get_settings()


def ensure_notifications_table() -> None:
    """
    Create the `notifications` table if it does not already exist.

    Safe to call multiple times -- it is a no-op when the table is present.
    """
    try:
        async_url = DBConfig().get_db_url()
        sync_url = async_url.replace("postgresql+asyncpg://", "postgresql://")

        engine = create_engine(sync_url, echo=False)

        with engine.connect() as conn:
            inspector = inspect(engine)
            existing_tables = inspector.get_table_names(schema=settings.db_schema)

            if "notifications" not in existing_tables:
                logger.info("Creating 'notifications' table...")
                conn.execute(
                    text(f"""
                    CREATE TABLE IF NOT EXISTS {settings.db_schema}.notifications (
                        id              SERIAL PRIMARY KEY,
                        user_id         INTEGER NOT NULL
                                            REFERENCES {settings.db_schema}."user"(user_id)
                                            ON DELETE CASCADE,
                        title           VARCHAR(255) NOT NULL,
                        message         TEXT,
                        type            VARCHAR(50) NOT NULL DEFAULT 'info',
                        link            VARCHAR(500),
                        metadata_json   TEXT,
                        is_read         BOOLEAN NOT NULL DEFAULT FALSE,
                        created_at      TIMESTAMPTZ DEFAULT NOW()
                    );
                """)
                )
                # Composite index for fast lookup by user + read status
                conn.execute(
                    text(f"""
                    CREATE INDEX IF NOT EXISTS ix_notifications_user_read
                    ON {settings.db_schema}.notifications (user_id, is_read);
                """)
                )
                conn.commit()
                logger.info("'notifications' table created successfully.")
            else:
                logger.debug(
                    "'notifications' table already exists -- skipping creation."
                )

        engine.dispose()

    except Exception as exc:
        logger.error(
            "Failed to ensure 'notifications' table: %s", exc, exc_info=True
        )

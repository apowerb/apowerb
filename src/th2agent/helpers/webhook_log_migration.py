from th2agent.configs.th2logger import setup_logging
from sqlalchemy import create_engine, inspect, text

from th2agent.helpers.database_connection import DBConfig
from th2agent.configs.settings import get_settings

logger = setup_logging(__name__)
settings = get_settings()


def ensure_webhook_logs_table() -> None:
    """
    Create the `webhook_logs` table if it does not already exist.

    Safe to call multiple times -- it is a no-op when the table is present.
    """
    try:
        async_url = DBConfig().get_db_url()
        sync_url = async_url.replace("postgresql+asyncpg://", "postgresql://")

        engine = create_engine(sync_url, echo=False)

        with engine.connect() as conn:
            inspector = inspect(engine)
            existing_tables = inspector.get_table_names(schema=settings.db_schema)

            if "webhook_logs" not in existing_tables:
                logger.info("Creating 'webhook_logs' table...")
                conn.execute(
                    text(f"""
                    CREATE TABLE IF NOT EXISTS {settings.db_schema}.webhook_logs (
                        id                  SERIAL PRIMARY KEY,
                        user_id             INTEGER NOT NULL
                                                REFERENCES {settings.db_schema}."user"(user_id)
                                                ON DELETE CASCADE,
                        subscription_id     INTEGER NOT NULL
                                                REFERENCES {settings.db_schema}.webhook_subscriptions(id)
                                                ON DELETE CASCADE,
                        agent_id            INTEGER NOT NULL,
                        trigger_event       VARCHAR(50) NOT NULL,
                        email_subject       VARCHAR(500),
                        email_sender        VARCHAR(500),
                        agent_message       TEXT,
                        agent_response      TEXT,
                        status              VARCHAR(20) NOT NULL DEFAULT 'pending',
                        error_message       TEXT,
                        created_at          TIMESTAMPTZ DEFAULT NOW(),
                        duration_ms         INTEGER
                    );
                """)
                )
                # Composite index for fast lookup by user + subscription
                conn.execute(
                    text(f"""
                    CREATE INDEX IF NOT EXISTS ix_webhook_logs_user_sub
                    ON {settings.db_schema}.webhook_logs (user_id, subscription_id);
                """)
                )
                conn.commit()
                logger.info("'webhook_logs' table created successfully.")
            else:
                logger.debug(
                    "'webhook_logs' table already exists -- skipping creation."
                )

        engine.dispose()

    except Exception as exc:
        logger.error(
            "Failed to ensure 'webhook_logs' table: %s", exc, exc_info=True
        )

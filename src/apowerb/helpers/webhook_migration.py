from apowerb.configs.th2logger import setup_logging
from sqlalchemy import create_engine, inspect, text

from apowerb.helpers.database_connection import DBConfig
from apowerb.configs.settings import get_settings

logger = setup_logging(__name__)
settings = get_settings()


def ensure_webhook_subscriptions_table() -> None:
    """
    Create the `webhook_subscriptions` table if it does not already exist.

    Safe to call multiple times -- it is a no-op when the table is present.
    """
    try:
        async_url = DBConfig().get_db_url()
        sync_url = async_url.replace("postgresql+asyncpg://", "postgresql://")

        engine = create_engine(sync_url, echo=False)

        with engine.connect() as conn:
            inspector = inspect(engine)
            existing_tables = inspector.get_table_names(schema=settings.db_schema)

            if "webhook_subscriptions" not in existing_tables:
                logger.info("Creating 'webhook_subscriptions' table...")
                conn.execute(
                    text(f"""
                    CREATE TABLE IF NOT EXISTS {settings.db_schema}.webhook_subscriptions (
                        id                      SERIAL PRIMARY KEY,
                        user_id                 INTEGER NOT NULL
                                                    REFERENCES {settings.db_schema}."user"(user_id)
                                                    ON DELETE CASCADE,
                        integration_id          INTEGER
                                                    REFERENCES {settings.db_schema}.integrations(id)
                                                    ON DELETE SET NULL,
                        provider                VARCHAR(50) NOT NULL DEFAULT 'microsoft_outlook',
                        subscription_id         VARCHAR(255) UNIQUE,
                        resource                VARCHAR(500) NOT NULL,
                        change_type             VARCHAR(100) NOT NULL DEFAULT 'created',
                        notification_url        VARCHAR(1000) NOT NULL,
                        client_state            VARCHAR(255) NOT NULL,
                        expiration_datetime     TIMESTAMPTZ,
                        agent_id                INTEGER NOT NULL,
                        agent_message_template  TEXT,
                        status                  VARCHAR(20) NOT NULL DEFAULT 'active',
                        last_notification_at    TIMESTAMPTZ,
                        last_history_id         VARCHAR(50),
                        created_at              TIMESTAMPTZ DEFAULT NOW(),
                        updated_at              TIMESTAMPTZ DEFAULT NOW()
                    );
                """)
                )
                # Composite index for fast subscription lookup
                conn.execute(
                    text(f"""
                    CREATE INDEX IF NOT EXISTS ix_webhook_subscriptions_user_provider_status
                    ON {settings.db_schema}.webhook_subscriptions (user_id, provider, status);
                """)
                )
                conn.commit()
                logger.info("'webhook_subscriptions' table created successfully.")
            else:
                logger.debug(
                    "'webhook_subscriptions' table already exists -- skipping creation."
                )

        engine.dispose()

    except Exception as exc:
        logger.error(
            "Failed to ensure 'webhook_subscriptions' table: %s", exc, exc_info=True
        )


def ensure_webhook_subscriptions_columns() -> None:
    """
    Add missing columns to the `webhook_subscriptions` table.

    Safe to call multiple times -- each ALTER TABLE is guarded by an existence
    check so it is a no-op when the column is already present.

    Columns managed here:
        - last_history_id (VARCHAR 50, nullable) — Gmail history cursor; NULL for Outlook.
    """
    try:
        async_url = DBConfig().get_db_url()
        sync_url = async_url.replace("postgresql+asyncpg://", "postgresql://")

        engine = create_engine(sync_url, echo=False)

        with engine.connect() as conn:
            inspector = inspect(engine)
            existing_columns = {
                col["name"]
                for col in inspector.get_columns(
                    "webhook_subscriptions", schema=settings.db_schema
                )
            }

            if "last_history_id" not in existing_columns:
                logger.info(
                    "Adding column 'last_history_id' to 'webhook_subscriptions'..."
                )
                conn.execute(
                    text(
                        f"ALTER TABLE {settings.db_schema}.webhook_subscriptions "
                        f"ADD COLUMN IF NOT EXISTS last_history_id VARCHAR(50) NULL;"
                    )
                )
                conn.commit()
                logger.info("Column 'last_history_id' added successfully.")
            else:
                logger.debug(
                    "Column 'last_history_id' already exists -- skipping."
                )

        engine.dispose()

    except Exception as exc:
        logger.error(
            "Failed to ensure columns on 'webhook_subscriptions': %s",
            exc,
            exc_info=True,
        )

from apowerb.configs.th2logger import setup_logging
from sqlalchemy import create_engine, inspect, text

from apowerb.helpers.database_connection import DBConfig
from apowerb.configs.settings import get_settings

logger = setup_logging(__name__)
settings = get_settings()


def ensure_integrations_table() -> None:
    """
    Create the `integrations` table if it does not already exist.

    Safe to call multiple times — it is a no-op when the table is present.
    """
    try:
        # Build a synchronous URL from the async one (strip +asyncpg driver)
        async_url = DBConfig().get_db_url()
        sync_url = async_url.replace("postgresql+asyncpg://", "postgresql://")

        engine = create_engine(sync_url, echo=False)

        with engine.connect() as conn:
            inspector = inspect(engine)
            existing_tables = inspector.get_table_names(schema=settings.db_schema)

            if "integrations" not in existing_tables:
                logger.info("Creating 'integrations' table…")
                conn.execute(
                    text(f"""
                    CREATE TABLE IF NOT EXISTS {settings.db_schema}.integrations (
                        id                SERIAL PRIMARY KEY,
                        user_id           INTEGER NOT NULL
                                              REFERENCES {settings.db_schema}."user"(user_id)
                                              ON DELETE CASCADE,
                        provider          VARCHAR(50)  NOT NULL,
                        provider_user_id  VARCHAR,
                        provider_username VARCHAR,
                        access_token      VARCHAR,
                        refresh_token     VARCHAR,
                        scopes            VARCHAR,
                        meta              JSON,
                        created_at        TIMESTAMP DEFAULT NOW(),
                        updated_at        TIMESTAMP DEFAULT NOW(),
                        CONSTRAINT uq_integration_user_provider
                            UNIQUE (user_id, provider)
                    );
                """)
                )
                conn.commit()
                logger.info(" 'integrations' table created successfully.")
            else:
                logger.debug(
                    " 'integrations' table already exists — skipping creation."
                )

        engine.dispose()

    except Exception as exc:
        # Log but don't crash the app — the table might have been created
        # by a concurrent process or a previous migration tool.
        logger.error("Failed to ensure 'integrations' table: %s", exc, exc_info=True)

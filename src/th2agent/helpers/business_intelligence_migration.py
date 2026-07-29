from th2agent.configs.th2logger import setup_logging
from sqlalchemy import create_engine, inspect, text

from th2agent.helpers.database_connection import DBConfig
from th2agent.configs.settings import get_settings

logger = setup_logging(__name__)
settings = get_settings()


def ensure_business_intelligence_table() -> None:
    """
    Create the `business_intelligence` table if it does not already exist.

    Safe to call multiple times -- it is a no-op when the table is present.
    """
    try:
        # Same pattern as integrations_migration.py
        async_url = DBConfig().get_db_url()
        sync_url = async_url.replace("postgresql+asyncpg://", "postgresql://")
        engine = create_engine(sync_url, echo=False)

        with engine.connect() as conn:
            inspector = inspect(engine)
            existing_tables = inspector.get_table_names(schema=settings.db_schema)

            if "business_intelligence" not in existing_tables:
                logger.info("Creating 'business_intelligence' table...")
                conn.execute(
                    text(
                        f"""
                        CREATE TABLE IF NOT EXISTS {settings.db_schema}.business_intelligence (
                            id              VARCHAR(36) PRIMARY KEY,
                            name            VARCHAR(255) NOT NULL,
                            type            VARCHAR(20) NOT NULL,
                            children JSONB NOT NULL DEFAULT '[]'::jsonb,
                            parents         JSONB NOT NULL DEFAULT '[]'::jsonb,
                            owner           VARCHAR(255) NOT NULL,
                            organization_id VARCHAR(255) NOT NULL,
                            project_id      VARCHAR(255) NOT NULL DEFAULT 'thaink2',
                            permissions     JSONB NOT NULL DEFAULT '[]'::jsonb,
                            status          VARCHAR(30) NOT NULL DEFAULT 'active',
                            created_at      TIMESTAMPTZ DEFAULT NOW(),
                            updated_at      TIMESTAMPTZ DEFAULT NOW(),
                            CONSTRAINT uq_bi_name_type_org_project
                                UNIQUE (name, type, organization_id, project_id)
                        );

                        CREATE INDEX IF NOT EXISTS idx_bi_org_project
                            ON {settings.db_schema}.business_intelligence(organization_id, project_id);

                        CREATE INDEX IF NOT EXISTS idx_bi_type_org_project
                            ON {settings.db_schema}.business_intelligence(type, organization_id, project_id);

                        CREATE INDEX IF NOT EXISTS idx_bi_owner
                            ON {settings.db_schema}.business_intelligence(owner);
                        """
                    )
                )
                conn.commit()
                logger.info("'business_intelligence' table created successfully.")
            else:
                logger.debug("'business_intelligence' table already exists -- skipping creation.")
                # Ensure 'config' column exists (added for chart/dashboard persistence)
                columns = {c["name"] for c in inspector.get_columns("business_intelligence", schema=settings.db_schema)}
                if "config" not in columns:
                    logger.info("Adding 'config' column to 'business_intelligence' table...")
                    conn.execute(text(f"ALTER TABLE {settings.db_schema}.business_intelligence ADD COLUMN config JSONB"))
                    conn.commit()
                    logger.info("'config' column added successfully.")

        engine.dispose()

    except Exception as exc:
        logger.error("Failed to ensure 'business_intelligence' table: %s", exc, exc_info=True)


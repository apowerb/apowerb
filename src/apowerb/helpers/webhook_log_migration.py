from apowerb.configs.th2logger import setup_logging
from sqlalchemy import create_engine, inspect, text

from apowerb.helpers.database_connection import DBConfig
from apowerb.configs.settings import get_settings

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


# Colonnes ajoutees au modele APRES la creation initiale de la table.
#
# `ensure_webhook_logs_table` ne cree la table que si elle est absente : sur
# une base plus ancienne que ces colonnes, elle ne fait rien et le modele
# interroge des colonnes qui n'existent pas. Toute colonne ajoutee au modele
# WebhookLog doit etre ajoutee ici, avec son type SQL et NULL -- une colonne
# NOT NULL sans defaut echouerait sur une table qui a deja des lignes.
_COLONNES_AJOUTEES: tuple[tuple[str, str], ...] = (
    # Suivi et rejeu de l'execution.
    ("resource_id", "VARCHAR(500) NULL"),
    ("payload_json", "TEXT NULL"),
    ("force_reprocess", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("next_attempt_at", "TIMESTAMPTZ NULL"),
    ("started_at", "TIMESTAMPTZ NULL"),
    ("completed_at", "TIMESTAMPTZ NULL"),
    # Corps du message et pieces jointes.
    ("email_body_html", "TEXT NULL"),
    ("email_body_text", "TEXT NULL"),
    ("attachments", "JSON NULL"),
)


def ensure_webhook_logs_columns() -> None:
    """
    Add missing columns to the `webhook_logs` table.

    Safe to call multiple times -- each column is checked first and every
    ALTER is guarded by IF NOT EXISTS, so it is a no-op once applied.
    """
    try:
        async_url = DBConfig().get_db_url()
        sync_url = async_url.replace("postgresql+asyncpg://", "postgresql://")

        engine = create_engine(sync_url, echo=False)

        with engine.connect() as conn:
            inspector = inspect(engine)
            if "webhook_logs" not in inspector.get_table_names(
                schema=settings.db_schema
            ):
                # La table sera creee par ensure_webhook_logs_table, avec ses
                # colonnes d'origine ; rien a completer ici.
                logger.debug(
                    "'webhook_logs' table absent -- nothing to alter."
                )
                engine.dispose()
                return

            existing_columns = {
                col["name"]
                for col in inspector.get_columns(
                    "webhook_logs", schema=settings.db_schema
                )
            }

            manquantes = [
                (nom, ddl)
                for nom, ddl in _COLONNES_AJOUTEES
                if nom not in existing_columns
            ]
            if not manquantes:
                logger.debug(
                    "'webhook_logs' has every managed column -- skipping."
                )
                engine.dispose()
                return

            for nom, ddl in manquantes:
                logger.info("Adding column '%s' to 'webhook_logs'...", nom)
                conn.execute(
                    text(
                        f"ALTER TABLE {settings.db_schema}.webhook_logs "
                        f"ADD COLUMN IF NOT EXISTS {nom} {ddl};"
                    )
                )
            conn.commit()
            logger.info(
                "Added %d column(s) to 'webhook_logs': %s",
                len(manquantes),
                ", ".join(nom for nom, _ in manquantes),
            )

        engine.dispose()

    except Exception as exc:
        logger.error(
            "Failed to ensure 'webhook_logs' columns: %s", exc, exc_info=True
        )

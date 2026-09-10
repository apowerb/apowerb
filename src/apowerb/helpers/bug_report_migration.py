"""Table des signalements de bug, créée au démarrage.

Même forme que ses voisines (`notification_migration`, `webhook_migration`) :
idempotente, appelée par `bootstrap()`, sans effet sur un déploiement où
la table existe déjà.

Deux choix de schéma valent d'être dits :

- ``user_id`` est ``ON DELETE SET NULL``, pas ``CASCADE``. Un compte
  supprimé ne doit pas emporter les bugs qu'il a signalés : le défaut,
  lui, est toujours là. ``reporter_email`` est recopié pour la même
  raison — savoir *qui* pouvait le reproduire survit au compte.
- ``duplicate_of`` plutôt qu'un simple compteur : chaque signalement
  garde ses propres logs et sa propre capture. Le deuxième témoin d'un
  défaut apporte souvent l'information qui manquait au premier, et un
  compteur seul l'aurait jetée.
"""

from apowerb.configs.th2logger import setup_logging
from sqlalchemy import create_engine, inspect, text

from apowerb.configs.settings import get_settings
from apowerb.helpers.database_connection import DBConfig

logger = setup_logging(__name__)
settings = get_settings()


def ensure_bug_reports_table() -> None:
    """Crée la table `bug_reports` si elle n'existe pas. Sûr à répéter."""
    try:
        async_url = DBConfig().get_db_url()
        sync_url = async_url.replace("postgresql+asyncpg://", "postgresql://")
        engine = create_engine(sync_url, echo=False)

        with engine.connect() as conn:
            inspector = inspect(engine)
            existing = inspector.get_table_names(schema=settings.db_schema)

            if "bug_reports" not in existing:
                logger.info("Creating 'bug_reports' table...")
                conn.execute(
                    text(f"""
                    CREATE TABLE IF NOT EXISTS {settings.db_schema}.bug_reports (
                        id               SERIAL PRIMARY KEY,
                        user_id          INTEGER
                                             REFERENCES {settings.db_schema}."user"(user_id)
                                             ON DELETE SET NULL,
                        reporter_email   VARCHAR(320),
                        title            VARCHAR(200) NOT NULL,
                        where_i_was      TEXT,
                        what_i_did       TEXT,
                        expected         TEXT,
                        observed         TEXT,
                        area             VARCHAR(40)  NOT NULL DEFAULT 'other',
                        severity         VARCHAR(20)  NOT NULL DEFAULT 'major',
                        status           VARCHAR(30)  NOT NULL DEFAULT 'new',
                        fingerprint      VARCHAR(32)  NOT NULL,
                        occurrences      INTEGER      NOT NULL DEFAULT 1,
                        duplicate_of     INTEGER
                                             REFERENCES {settings.db_schema}.bug_reports(id)
                                             ON DELETE SET NULL,
                        route            VARCHAR(512),
                        run_id           VARCHAR(100),
                        server_version   VARCHAR(50),
                        context_json     TEXT,
                        api_calls_json   TEXT,
                        console_json     TEXT,
                        server_logs_json TEXT,
                        request_ids_json TEXT,
                        screenshot_path  VARCHAR(500),
                        issue_url        VARCHAR(500),
                        issue_number     INTEGER,
                        admin_note       TEXT,
                        created_at       TIMESTAMPTZ DEFAULT NOW(),
                        updated_at       TIMESTAMPTZ DEFAULT NOW()
                    );
                """)
                )
                # L'écran de triage lit « les non traités, du plus récent au
                # plus ancien » et la déduplication cherche par empreinte.
                # Ce sont les deux seules requêtes chaudes.
                conn.execute(
                    text(f"""
                    CREATE INDEX IF NOT EXISTS ix_bug_reports_status_created
                    ON {settings.db_schema}.bug_reports (status, created_at DESC);
                """)
                )
                conn.execute(
                    text(f"""
                    CREATE INDEX IF NOT EXISTS ix_bug_reports_fingerprint
                    ON {settings.db_schema}.bug_reports (fingerprint);
                """)
                )
                conn.commit()
                logger.info("'bug_reports' table created successfully.")
            else:
                # Colonne arrivée après la première version de la table.
                # ADD COLUMN IF NOT EXISTS : gratuit quand elle est là,
                # et évite une seconde fonction de migration pour un champ.
                conn.execute(
                    text(f"""
                    ALTER TABLE {settings.db_schema}.bug_reports
                    ADD COLUMN IF NOT EXISTS where_i_was TEXT,
                    ADD COLUMN IF NOT EXISTS area VARCHAR(40) NOT NULL DEFAULT 'other';
                """)
                )
                conn.commit()
                logger.debug("'bug_reports' table already exists -- columns ensured.")

        engine.dispose()
    except Exception as exc:
        # Comme ses voisines : une migration qui lève au boot rendrait le
        # serveur inutilisable pour une fonctionnalité annexe.
        logger.error("Failed to ensure 'bug_reports' table: %s", exc)


__all__ = ["ensure_bug_reports_table"]

"""Les deux tables de la configuration posée depuis l'écran.

Branchée sur ``bootstrap()`` comme ``ensure_admin_tables``, jamais à
l'import : importer ``apowerb`` ne doit pas toucher une base partagée.
"""

from __future__ import annotations

from apowerb.configs.settings import get_settings
from apowerb.configs.th2logger import setup_logging
from apowerb.helpers.database_connection import DBConfig
from sqlalchemy import create_engine, inspect, text

logger = setup_logging(__name__)


def ensure_config_admin_tables() -> None:
    """Crée les tables absentes. Idempotent, chacune vérifiée séparément."""
    try:
        _create(get_settings().db_schema)
    except Exception as exc:  # noqa: BLE001 -- bruyant, jamais fatal au boot
        # Même règle que ``ensure_admin_tables`` : une migration ratée coûte
        # cet écran, pas la plateforme. L'API répondra en erreur sur ses
        # propres routes, ce qui se diagnostique ; un service qui refuse de
        # démarrer parce qu'un panneau d'administration manque, non.
        logger.error("Could not ensure the configuration tables: %s", exc)


def _create(schema: str) -> None:
    sync_url = DBConfig().get_db_url().replace("postgresql+asyncpg://", "postgresql://")
    engine = create_engine(sync_url, echo=False)

    with engine.connect() as conn:
        present = set(inspect(engine).get_table_names(schema=schema))

        # ``value_enc`` porte du chiffré Fernet, jamais autre chose : le store
        # refuse d'écrire quand ENCRYPT_KEY manque plutôt que de retomber en
        # clair (même règle que les jetons d'intégration, B7). La colonne est
        # TEXT parce qu'un chiffré Fernet est plus long que sa source, pas
        # parce que la valeur le serait.
        if "admin_config_variable" not in present:
            logger.info("Creating 'admin_config_variable' table...")
            conn.execute(text(f"""
                CREATE TABLE IF NOT EXISTS {schema}.admin_config_variable (
                    name       VARCHAR(200) PRIMARY KEY,
                    value_enc  TEXT NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_by VARCHAR(255) NOT NULL
                )
            """))

        # L'historique. La ligne ci-dessus dit qui a posé la valeur ACTUELLE ;
        # elle ne dit pas qu'une clé a été remplacée trois fois hier. Une table
        # plutôt que le seul logger ``apowerb.audit`` : un journal tourne, et
        # « qui a changé quoi » est précisément la question qu'on se pose après
        # la rotation. Aucune valeur n'y entre, ni en clair ni chiffrée.
        if "admin_config_audit" not in present:
            logger.info("Creating 'admin_config_audit' table...")
            conn.execute(text(f"""
                CREATE TABLE IF NOT EXISTS {schema}.admin_config_audit (
                    id     BIGSERIAL PRIMARY KEY,
                    name   VARCHAR(200) NOT NULL,
                    action VARCHAR(20) NOT NULL,
                    actor  VARCHAR(255) NOT NULL,
                    at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """))
            conn.execute(text(
                f"CREATE INDEX IF NOT EXISTS admin_config_audit_at_idx "
                f"ON {schema}.admin_config_audit (at DESC)"
            ))

        conn.commit()

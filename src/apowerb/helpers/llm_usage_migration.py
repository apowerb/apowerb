from apowerb.configs.th2logger import setup_logging
from sqlalchemy import create_engine, inspect, text

from apowerb.helpers.database_connection import DBConfig
from apowerb.configs.settings import get_settings

logger = setup_logging(__name__)
settings = get_settings()


def ensure_llm_usage_table() -> None:
    """
    Create the `llm_usage` table if it does not already exist.

    Safe to call multiple times -- it is a no-op when the table is present.
    """
    try:
        async_url = DBConfig().get_db_url()
        sync_url = async_url.replace("postgresql+asyncpg://", "postgresql://")
        engine = create_engine(sync_url, echo=False)

        with engine.connect() as conn:
            inspector = inspect(engine)
            existing_tables = inspector.get_table_names(schema=settings.db_schema)

            if "llm_usage" not in existing_tables:
                logger.info("Creating 'llm_usage' table...")
                conn.execute(
                    text(
                        f"""
                        CREATE TABLE IF NOT EXISTS {settings.db_schema}.llm_usage (
                            id                  SERIAL PRIMARY KEY,
                            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            agent_id            INTEGER NOT NULL,
                            agent_name          VARCHAR(255) NOT NULL,
                            owner_id            VARCHAR(255),
                            session_id          VARCHAR(255),
                            invocation_id       VARCHAR(255),
                            tool_names          TEXT,
                            invocation_source   VARCHAR(255),
                            model               VARCHAR(255) NOT NULL,
                            input_tokens        INTEGER NOT NULL DEFAULT 0,
                            output_tokens       INTEGER NOT NULL DEFAULT 0,
                            thoughts_tokens     INTEGER NOT NULL DEFAULT 0,
                            cached_tokens       INTEGER NOT NULL DEFAULT 0,
                            total_tokens        INTEGER NOT NULL DEFAULT 0
                        );

                        CREATE INDEX IF NOT EXISTS ix_llm_usage_agent_created
                            ON {settings.db_schema}.llm_usage(agent_id, created_at);
                        """
                    )
                )
                conn.commit()
                logger.info("'llm_usage' table created successfully.")
            else:
                logger.debug("'llm_usage' table already exists -- skipping creation.")

            # Added after the table shipped to prod/DEV -- CREATE TABLE IF
            # NOT EXISTS above never re-runs on an existing table, so this
            # index needs its own unconditional CREATE INDEX IF NOT EXISTS.
            conn.execute(
                text(
                    f"""
                    CREATE INDEX IF NOT EXISTS ix_llm_usage_owner_created
                        ON {settings.db_schema}.llm_usage(owner_id, created_at);
                    """
                )
            )
            conn.commit()

            # Drivers instrumentation (2026-07-20): answers "what makes the
            # tokens burn". Additive and nullable -- rows written before
            # this ships keep NULLs, and every drivers query tolerates
            # them (it filters on invocation_id IS NOT NULL rather than
            # assuming coverage). Same reason as the index above: the
            # CREATE TABLE branch never re-runs, so these need their own
            # unconditional idempotent statements.
            #
            # Why a plain CREATE INDEX and not CONCURRENTLY, on a table
            # that takes continuous writes: a plain CREATE INDEX holds a
            # SHARE lock that blocks INSERTs for the duration of the
            # build, and llm_usage grows without bound -- so the concern
            # is legitimate. It is bounded here by IF NOT EXISTS: the
            # index is BUILT EXACTLY ONCE, at the first boot after this
            # ships, and every later boot is a no-op. Measured on
            # 2026-07-20, right before shipping, on the two live databases
            # of the day: 7 rows (80 kB) and 30 rows. Sub-millisecond.
            # CONCURRENTLY would also have to run outside this
            # transaction, on its own autocommit connection.
            # ⚠️ Do NOT copy this pattern for an index added to
            # llm_usage LATER: by then the table is large and the lock is
            # real. Measure first, then use CONCURRENTLY.
            #
            # The ADD COLUMNs are safe regardless: a nullable column with
            # no default is a catalog-only change on PG11+, no table
            # rewrite.
            conn.execute(
                text(
                    f"""
                    ALTER TABLE {settings.db_schema}.llm_usage
                        ADD COLUMN IF NOT EXISTS invocation_id VARCHAR(255);
                    ALTER TABLE {settings.db_schema}.llm_usage
                        ADD COLUMN IF NOT EXISTS tool_names TEXT;

                    CREATE INDEX IF NOT EXISTS ix_llm_usage_invocation
                        ON {settings.db_schema}.llm_usage(invocation_id, id);
                    """
                )
            )
            conn.commit()

            # Quota du modele mutualise (2026-07-27) : marque les tours
            # payes par la cle thaink2, seuls plafonnes. FALSE pour tout
            # l'historique -- aucune ligne anterieure ne passait par ce
            # modele, qui n'existait pas.
            #
            # ADD COLUMN ... NOT NULL DEFAULT FALSE est catalogue-only sur
            # PG11+ (le defaut non volatil est stocke en metadonnee) : pas
            # de reecriture de table, donc pas de verrou long malgre la
            # taille de llm_usage.
            #
            # AUCUN index ajoute ici, volontairement : la requete de quota
            # filtre (owner_id, created_at) et utilise donc l'index
            # ix_llm_usage_owner_created deja present ; `billed_to_thaink2`
            # n'est qu'un filtre residuel. C'est exactement le cas contre
            # lequel l'avertissement ci-dessus met en garde -- un CREATE
            # INDEX non-CONCURRENTLY sur cette table, maintenant qu'elle a
            # grossi, bloquerait les INSERTs le temps du build.
            conn.execute(
                text(
                    f"""
                    ALTER TABLE {settings.db_schema}.llm_usage
                        ADD COLUMN IF NOT EXISTS billed_to_thaink2
                        BOOLEAN NOT NULL DEFAULT FALSE;
                    """
                )
            )
            conn.commit()

        engine.dispose()

    except Exception as exc:
        logger.error("Failed to ensure 'llm_usage' table: %s", exc, exc_info=True)

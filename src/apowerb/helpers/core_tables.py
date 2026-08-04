"""Creation of the tables declared in ``apowerb.models``.

Every other ``ensure_*`` migration in the boot sequence assumes those tables are
already there: ``ensure_user_columns`` adds columns to ``user``, and
``ensure_integrations_table`` declares a foreign key onto ``user(user_id)``. On
an existing deployment they are, because the schema predates the migrations. On
a brand-new database -- the Docker Compose stack of apowerb-hosting, a fresh
self-hosted install -- nothing ever creates them, so ``user`` is missing, the
migrations that depend on it fail, and creating an account answers a 500.

``create_all(checkfirst=True)`` is idempotent: it emits DDL only for what is
absent, and never alters an existing table. An established deployment therefore
sees a no-op -- in particular no index is ever added to a table that already
exists.
"""

from sqlalchemy import create_engine

from apowerb.configs.settings import get_settings
from apowerb.configs.th2logger import setup_logging
from apowerb.helpers.database_connection import DBConfig

logger = setup_logging(__name__)
settings = get_settings()


def ensure_core_tables(engine=None) -> None:
    """Create the missing tables of the core model. Safe to call repeatedly.

    ``engine`` is only there for the tests; at boot the engine is built from the
    configured database URL.
    """
    try:
        # Importing the module is what registers the mappers on ``Base.metadata``.
        import apowerb.models  # noqa: F401
        from apowerb.helpers.database import Base

        own_engine = engine is None
        if own_engine:
            async_url = DBConfig().get_db_url()
            sync_url = async_url.replace("postgresql+asyncpg://", "postgresql://")
            engine = create_engine(sync_url, echo=False)

        try:
            before = set(Base.metadata.tables)
            Base.metadata.create_all(engine, checkfirst=True)
            logger.info(
                "Core tables ensured (%d declared, schema '%s').",
                len(before),
                settings.db_schema,
            )
        finally:
            if own_engine:
                engine.dispose()

    except Exception as exc:
        # Same contract as the other boot migrations: log, do not crash. A
        # concurrent process may have created the tables in the meantime.
        logger.error("Failed to ensure the core tables: %s", exc, exc_info=True)

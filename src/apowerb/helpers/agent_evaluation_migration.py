"""Boot-time creation of the `agent_evaluation_results` table.

Same idempotent shape as `business_intelligence_migration.py`: build a sync
engine, check whether the table exists, create it if not, log either way,
never raise -- a migration failure must not take the whole boot down with
it. Unlike that module this one does not hand-write DDL: `EvaluationResult`
(evaluation/models.py) is already declared with SQLAlchemy's declarative
Base, so `Table.create(checkfirst=True)` creates exactly what the ORM
model says, with no second copy of the column list to drift out of sync.
Purely additive -- no other table is read or altered.
"""

from sqlalchemy import create_engine, inspect

from apowerb.configs.settings import get_settings
from apowerb.configs.th2logger import setup_logging
from apowerb.helpers.database_connection import DBConfig

logger = setup_logging(__name__)
settings = get_settings()


def ensure_agent_evaluation_table(engine=None) -> None:
    """Create the `agent_evaluation_results` table if it does not exist.

    Safe to call multiple times -- a no-op once the table is present.
    `engine` is only there for tests; at boot it is built from the
    configured database URL, same as the other `ensure_*` migrations.
    """
    try:
        from apowerb.evaluation.models import EvaluationResult

        table_name = EvaluationResult.__tablename__

        own_engine = engine is None
        if own_engine:
            async_url = DBConfig().get_db_url()
            sync_url = async_url.replace("postgresql+asyncpg://", "postgresql://")
            engine = create_engine(sync_url, echo=False)

        try:
            inspector = inspect(engine)
            existing_tables = inspector.get_table_names(schema=settings.db_schema)

            if table_name not in existing_tables:
                logger.info("Creating '%s' table...", table_name)
                EvaluationResult.__table__.create(bind=engine, checkfirst=True)
                logger.info("'%s' table created successfully.", table_name)
            else:
                logger.debug(
                    "'%s' table already exists -- skipping creation.", table_name
                )
        finally:
            if own_engine:
                engine.dispose()

    except Exception as exc:
        logger.error(
            "Failed to ensure 'agent_evaluation_results' table: %s",
            exc,
            exc_info=True,
        )

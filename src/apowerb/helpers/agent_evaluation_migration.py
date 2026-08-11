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

from sqlalchemy import create_engine, inspect, text

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


def ensure_agent_evaluation_run_id_column(engine=None) -> None:
    """Add `run_id` to `agent_evaluation_results` and backfill it.

    Purely additive, same idempotent shape as `ensure_agent_evaluation_table`:
    a no-op once the column is present. A fresh install never reaches the
    ALTER path -- `ensure_agent_evaluation_table()` above already creates the
    table from the current `EvaluationResult` model, which declares `run_id`
    NOT NULL -- this function only matters for a deployment where the table
    predates the column.

    The six results of one evaluation share no explicit link today; they
    come from a single `run_and_persist()` transaction and so share
    `(session_id, created_at)` to the microsecond. That pair is exactly what
    the backfill groups by, done here explicitly rather than left for a
    reader to infer at query time later. `gen_random_uuid()` has been a
    PostgreSQL core function (no `pgcrypto` extension required) since
    version 13.
    """
    try:
        table_name = "agent_evaluation_results"
        schema = settings.db_schema

        own_engine = engine is None
        if own_engine:
            async_url = DBConfig().get_db_url()
            sync_url = async_url.replace("postgresql+asyncpg://", "postgresql://")
            engine = create_engine(sync_url, echo=False)

        try:
            inspector = inspect(engine)
            existing_columns = {
                col["name"] for col in inspector.get_columns(table_name, schema=schema)
            }

            if "run_id" in existing_columns:
                logger.debug("Column 'run_id' already exists on '%s' -- skipping.", table_name)
                return

            logger.info("Adding column 'run_id' to '%s'...", table_name)
            qualified = f'"{schema}".{table_name}'
            with engine.connect() as conn:
                conn.execute(text(f"ALTER TABLE {qualified} ADD COLUMN run_id UUID NULL"))
                conn.execute(
                    text(
                        f"""
                        WITH groups AS (
                            SELECT DISTINCT session_id, created_at,
                                   gen_random_uuid() AS new_run_id
                            FROM {qualified}
                            WHERE run_id IS NULL
                        )
                        UPDATE {qualified} r
                        SET run_id = g.new_run_id
                        FROM groups g
                        WHERE r.session_id = g.session_id
                          AND r.created_at = g.created_at
                          AND r.run_id IS NULL
                        """
                    )
                )
                conn.execute(text(f"ALTER TABLE {qualified} ALTER COLUMN run_id SET NOT NULL"))
                conn.execute(
                    text(f"CREATE INDEX IF NOT EXISTS ix_agent_eval_run_id ON {qualified} (run_id)")
                )
                conn.commit()
            logger.info("Column 'run_id' added and backfilled successfully.")
        finally:
            if own_engine:
                engine.dispose()

    except Exception as exc:
        logger.error(
            "Failed to ensure 'run_id' column on 'agent_evaluation_results': %s",
            exc,
            exc_info=True,
        )

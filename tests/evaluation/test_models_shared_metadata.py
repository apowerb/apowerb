"""Regression test: importing this module must not break ensure_core_tables.

`Base.metadata` (helpers/database.py) is shared process-wide by every
declarative model in the project. `EvaluationResult` is the first model on
it to use a Postgres-only column type (`dialects.postgresql.JSONB`) --
before this test existed, that was safe only because nothing under
`tests/` imported `apowerb.evaluation.models`. Once something does (as
`run_service.py` now unavoidably does), `ensure_core_tables()`'s SQLite
fixture (tests/test_core_tables.py) would try to compile a `jsonb` column
via the SQLite dialect and raise `CompileError` -- taking down the
`user`/`integrations` table creation test for a completely unrelated
model. See evaluation/models.py's `_DETAILS_TYPE` for the fix.
"""

from sqlalchemy import create_engine, inspect
from sqlalchemy.pool import StaticPool

from apowerb.configs.settings import get_settings
from apowerb.evaluation.models import EvaluationResult  # noqa: F401 -- registers on Base.metadata
from apowerb.helpers.core_tables import ensure_core_tables

SCHEMA = get_settings().db_schema or None


def test_ensure_core_tables_still_works_once_evaluation_models_is_imported():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    if SCHEMA:
        with engine.begin() as conn:
            conn.exec_driver_sql(f'ATTACH DATABASE \':memory:\' AS "{SCHEMA}"')

    ensure_core_tables(engine=engine)  # must not raise CompileError

    tables = inspect(engine).get_table_names(schema=SCHEMA)
    assert "user" in tables
    assert "agent_evaluation_results" in tables

    engine.dispose()

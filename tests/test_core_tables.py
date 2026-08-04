"""A brand-new database must end up with the tables of the core model.

Without this, `user` is never created: every other boot migration only adds
columns to it or references it, so a fresh install answers 500 on sign-up.
"""

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.pool import StaticPool

from apowerb.configs.settings import get_settings
from apowerb.helpers.core_tables import ensure_core_tables

# Empty in the unit workflow, "public" against a real database. When it is set,
# the models qualify their tables with it and SQLite needs that name attached.
SCHEMA = get_settings().db_schema or None


@pytest.fixture
def sqlite_engine():
    """In-memory SQLite standing in for the target schema.

    StaticPool keeps a single connection: otherwise the ATTACH would only hold
    for the first one and the qualified tables would vanish.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    if SCHEMA:
        with engine.begin() as conn:
            conn.exec_driver_sql(f'ATTACH DATABASE \':memory:\' AS "{SCHEMA}"')
    yield engine
    engine.dispose()


def _tables(engine):
    return inspect(engine).get_table_names(schema=SCHEMA)


def test_creates_the_user_table_on_an_empty_database(sqlite_engine):
    assert "user" not in _tables(sqlite_engine)

    ensure_core_tables(engine=sqlite_engine)

    tables = _tables(sqlite_engine)
    assert "user" in tables
    assert "integrations" in tables


def test_is_idempotent(sqlite_engine):
    ensure_core_tables(engine=sqlite_engine)
    before = sorted(_tables(sqlite_engine))

    ensure_core_tables(engine=sqlite_engine)

    assert sorted(_tables(sqlite_engine)) == before

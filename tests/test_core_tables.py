"""A brand-new database must end up with the tables of the core model.

Without this, `user` is never created: every other boot migration only adds
columns to it or references it, so a fresh install answers 500 on sign-up.
"""

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.pool import StaticPool

from apowerb.configs.settings import get_settings
from apowerb.helpers.core_tables import ensure_core_tables


@pytest.fixture
def sqlite_engine():
    """In-memory SQLite standing in for the target schema.

    The project MetaData carries ``schema=<DB_SCHEMA>``, so the tables are
    emitted as ``public.user`` and SQLite needs that name attached. StaticPool
    keeps a single connection, otherwise the ATTACH only holds for the first.
    """
    schema = get_settings().db_schema
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        conn.exec_driver_sql(f"ATTACH DATABASE ':memory:' AS {schema}")
    yield engine
    engine.dispose()


def test_creates_the_user_table_on_an_empty_database(sqlite_engine):
    schema = get_settings().db_schema
    assert "user" not in inspect(sqlite_engine).get_table_names(schema=schema)

    ensure_core_tables(engine=sqlite_engine)

    tables = inspect(sqlite_engine).get_table_names(schema=schema)
    assert "user" in tables
    assert "integrations" in tables


def test_is_idempotent(sqlite_engine):
    ensure_core_tables(engine=sqlite_engine)
    before = sorted(inspect(sqlite_engine).get_table_names(schema=get_settings().db_schema))

    ensure_core_tables(engine=sqlite_engine)

    after = sorted(inspect(sqlite_engine).get_table_names(schema=get_settings().db_schema))
    assert before == after

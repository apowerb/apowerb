"""PostgreSQL connections for ``tool_run_sql``.

Live regression 2026-10-12 (agent-dev): every ``tool_run_sql`` call on a
PostgreSQL config failed with ``'psycopg2.extensions.connection' object has
no attribute '_cursor_factory' and no __dict__ for setting new attributes``.
The connection helpers stored ``RealDictCursor`` on the connection object,
which psycopg2's C type refuses; the cursor factory is already passed to
``conn.cursor()``. The fake connection below has ``__slots__`` like the real
one, so any attribute assignment on it fails the same way.
"""
from __future__ import annotations

import psycopg2
import pytest

from apowerb.tools_store.portfolio import database


class _SlottedCursor:
    __slots__ = ("rowcount",)

    def __init__(self):
        self.rowcount = 1

    def execute(self, sql):
        pass

    def fetchall(self):
        return [{"ok": 1}]

    def close(self):
        pass


class _SlottedConnection:
    __slots__ = ()

    def cursor(self, cursor_factory=None):
        return _SlottedCursor()

    def commit(self):
        pass

    def close(self):
        pass


@pytest.fixture
def slotted_connect(monkeypatch):
    monkeypatch.setattr(psycopg2, "connect", lambda **kwargs: _SlottedConnection())


def test_env_connection_does_not_set_attributes_on_the_connection(slotted_connect, monkeypatch):
    monkeypatch.setenv("DB_TYPE", "postgresql")
    monkeypatch.setenv("DB_NAME", "demo")
    monkeypatch.setenv("DB_USER", "reader")
    monkeypatch.setenv("DB_PASSWORD", "x")

    assert isinstance(database._get_connection(), _SlottedConnection)


def test_bound_tool_run_sql_reads_a_postgres_config(slotted_connect):
    tools = database.make_database_tools(
        "agent-test",
        db_params={"DB_TYPE": "postgresql", "DB_NAME": "demo", "DB_USER": "reader", "DB_PASSWORD": "x"},
    )
    run_sql = next(t for t in tools if t.__name__.startswith("tool_run_sql"))

    result = run_sql("SELECT 1 AS ok")

    assert result["success"] is True, result
    assert result["data"] == [{"ok": 1}]

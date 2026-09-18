"""Tests for ``apowerb.scheduler.events_retention``.

ADK's DatabaseSessionService keeps the ``events`` table forever; this
module purges old rows daily. These tests pin the contract: retention
parsing, schema-qualified + injection-proof target, and a single purge
pass that issues a parameterised DELETE with a sane cutoff and commits.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest

from apowerb.scheduler import events_retention as er


# ── retention window parsing ──────────────────────────────────────────
def test_retention_default_when_unset(monkeypatch):
    monkeypatch.delenv("ADK_EVENTS_RETENTION_DAYS", raising=False)
    assert er._retention_days() == 90


def test_retention_env_override(monkeypatch):
    monkeypatch.setenv("ADK_EVENTS_RETENTION_DAYS", "30")
    assert er._retention_days() == 30


@pytest.mark.parametrize("bad", ["abc", "", "0", "-5"])
def test_retention_invalid_falls_back_to_90(monkeypatch, bad):
    monkeypatch.setenv("ADK_EVENTS_RETENTION_DAYS", bad)
    assert er._retention_days() == 90


# ── schema-qualified, injection-proof target ──────────────────────────
def test_events_table_is_schema_qualified(monkeypatch):
    monkeypatch.setattr(er, "get_settings", lambda: type("S", (), {"db_schema": "th2agent_dev"}))
    assert er._events_table() == "th2agent_dev.events"


@pytest.mark.parametrize("evil", ["public; drop table x", "a b", "", "1bad", "sch.ema"])
def test_events_table_rejects_unsafe_schema(monkeypatch, evil):
    monkeypatch.setattr(er, "get_settings", lambda: type("S", (), {"db_schema": evil}))
    with pytest.raises(ValueError):
        er._events_table()


# ── single purge pass ─────────────────────────────────────────────────
class _FakeResult:
    def __init__(self, rowcount, scalar=None):
        self.rowcount = rowcount
        self._scalar = scalar

    def scalar(self):
        return self._scalar


class _FakeDB:
    """Answers like Postgres about a table it may not have.

    ``to_regclass`` returns NULL for an unknown relation. Any other statement
    that names ``events`` while the table is absent raises, as asyncpg's
    UndefinedTableError would -- so a test can tell "skipped" from "tried".
    """

    def __init__(self, rowcount, *, table_exists=True):
        self._rowcount = rowcount
        self._table_exists = table_exists
        self.executed = []
        self.committed = False

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        self.executed.append((sql, params))
        if "to_regclass" in sql:
            return _FakeResult(0, scalar="events" if self._table_exists else None)
        if not self._table_exists and ".events" in sql:
            raise RuntimeError(f'relation "{sql}" does not exist')
        return _FakeResult(self._rowcount)

    async def commit(self):
        self.committed = True


def _wire_fake_db(monkeypatch, db):
    @asynccontextmanager
    async def _session():
        yield db

    monkeypatch.setattr(er.sessionmanager, "session", _session)
    monkeypatch.setattr(er, "get_settings", lambda: type("S", (), {"db_schema": "th2agent_dev"}))


async def test_purge_issues_parameterised_delete_and_commits(monkeypatch):
    monkeypatch.setenv("ADK_EVENTS_RETENTION_DAYS", "90")
    db = _FakeDB(rowcount=7)
    _wire_fake_db(monkeypatch, db)

    deleted = await er._purge_old_events()

    assert deleted == 7
    assert db.committed is True
    # the presence probe, one CREATE INDEX IF NOT EXISTS (idempotent), the DELETE
    assert len(db.executed) == 3
    probe_sql, probe_params = db.executed[0]
    assert "to_regclass" in probe_sql
    assert probe_params == {"qualified_name": "th2agent_dev.events"}
    index_sql, _ = db.executed[1]
    assert "CREATE INDEX IF NOT EXISTS" in index_sql
    assert "th2agent_dev.events" in index_sql
    sql, params = db.executed[2]
    assert "DELETE FROM th2agent_dev.events" in sql
    assert ":cutoff" in sql  # bound, not string-interpolated
    cutoff = params["cutoff"]
    expected = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=90)
    # cutoff is naive-UTC and ~90 days ago (allow a minute of clock drift)
    assert cutoff.tzinfo is None
    assert abs((cutoff - expected).total_seconds()) < 60


async def test_purge_returns_zero_when_nothing_deleted(monkeypatch):
    db = _FakeDB(rowcount=0)
    _wire_fake_db(monkeypatch, db)
    assert await er._purge_old_events() == 0
    assert db.committed is True


# ── fresh database: ADK has not created ``events`` yet ────────────────
async def test_purge_is_a_no_op_before_adk_created_the_table(monkeypatch):
    """ADK creates ``events`` lazily, on the first conversation. The loop runs
    at startup, so on every fresh install its first pass used to fail -- and
    log ``purge pass failed: relation "public.events" does not exist``. Nothing
    to purge is the true answer: no index, no DELETE, no exception.
    """
    db = _FakeDB(rowcount=0, table_exists=False)
    _wire_fake_db(monkeypatch, db)

    assert await er._purge_old_events() == 0

    assert [sql for sql, _ in db.executed if "to_regclass" not in sql] == []
    assert db.committed is False

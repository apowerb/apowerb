"""Unit tests for the opt-in psycopg2 connection pool (text_to_sql).

No real database: the pool factory and connections are faked. Covers config
parsing, pool-key derivation, get-or-create caching, and acquire/release
routing for both the pooled and non-pooled paths.
"""

from __future__ import annotations

import types

from th2agent.tools_store.portfolio import text_to_sql as tts


def _state(**kw):
    base = dict(db_type="postgresql", db_ok=True, db_host="h", db_port=5432,
                db_name="d", db_user="u")
    base.update(kw)
    return types.SimpleNamespace(**base)


class TestPoolConfig:
    def test_pool_enabled(self, monkeypatch):
        monkeypatch.delenv("SQL_POOL_ENABLED", raising=False)
        assert tts._pool_enabled() is False
        for v in ("1", "true", "YES"):
            monkeypatch.setenv("SQL_POOL_ENABLED", v)
            assert tts._pool_enabled() is True
        monkeypatch.setenv("SQL_POOL_ENABLED", "0")
        assert tts._pool_enabled() is False

    def test_pool_max(self, monkeypatch):
        monkeypatch.delenv("SQL_POOL_MAX", raising=False)
        assert tts._pool_max() == 4
        monkeypatch.setenv("SQL_POOL_MAX", "8")
        assert tts._pool_max() == 8
        monkeypatch.setenv("SQL_POOL_MAX", "garbage")
        assert tts._pool_max() == 4
        monkeypatch.setenv("SQL_POOL_MAX", "0")
        assert tts._pool_max() == 1     # floored to at least 1


class TestPoolKeyAndUse:
    def test_pg_pool_key(self):
        assert tts._pg_pool_key(_state()) == ("h", 5432, "d", "u")

    def test_use_pool_only_pg_enabled_dbok(self, monkeypatch):
        monkeypatch.setenv("SQL_POOL_ENABLED", "1")
        assert tts._use_pool(_state()) is True
        assert tts._use_pool(_state(db_type="mysql")) is False
        assert tts._use_pool(_state(db_ok=False)) is False
        monkeypatch.setenv("SQL_POOL_ENABLED", "0")
        assert tts._use_pool(_state()) is False


class TestGetOrCreatePool:
    def test_creates_once_per_key(self):
        tts._PG_POOLS.clear()
        calls = {"n": 0}

        def creator():
            calls["n"] += 1
            return object()

        p1 = tts._get_or_create_pool(("k",), creator)
        p2 = tts._get_or_create_pool(("k",), creator)
        assert p1 is p2 and calls["n"] == 1          # cached, created once
        p3 = tts._get_or_create_pool(("other",), creator)
        assert p3 is not p1 and calls["n"] == 2      # distinct key -> new pool


class TestAcquireRelease:
    def test_pooled_path(self, monkeypatch):
        monkeypatch.setenv("SQL_POOL_ENABLED", "1")
        fake_conn = types.SimpleNamespace(autocommit=False)

        class FakePool:
            def __init__(self):
                self.put = []

            def getconn(self):
                return fake_conn

            def putconn(self, c):
                self.put.append(c)

        fp = FakePool()
        monkeypatch.setattr(tts, "_get_state", lambda name: _state())
        monkeypatch.setattr(tts, "_get_pg_pool", lambda s: fp)

        conn = tts._acquire_conn("agentX")
        assert conn is fake_conn
        assert fake_conn.autocommit is True          # SELECT-only safety
        tts._release_conn("agentX", conn)
        assert fp.put == [fake_conn]                  # returned to pool, not closed

    def test_non_pooled_path_closes(self, monkeypatch):
        monkeypatch.setenv("SQL_POOL_ENABLED", "0")
        closed = {"n": 0}
        fake_conn = types.SimpleNamespace(
            close=lambda: closed.__setitem__("n", closed["n"] + 1))
        monkeypatch.setattr(tts, "_get_state", lambda name: _state())
        monkeypatch.setattr(tts, "_agent_connection", lambda name: fake_conn)

        conn = tts._acquire_conn("agentX")
        assert conn is fake_conn
        tts._release_conn("agentX", conn)
        assert closed["n"] == 1                       # plain close, no pool

    def test_release_frees_slot_then_closes_on_putconn_error(self, monkeypatch):
        monkeypatch.setenv("SQL_POOL_ENABLED", "1")
        closed = {"n": 0}
        fake_conn = types.SimpleNamespace(
            close=lambda: closed.__setitem__("n", closed["n"] + 1))

        class FlakyPool:
            def __init__(self):
                self.calls = []

            def putconn(self, c, close=False):
                self.calls.append(close)      # record normal vs close=True
                raise RuntimeError("boom")

        fp = FlakyPool()
        monkeypatch.setattr(tts, "_get_state", lambda name: _state())
        monkeypatch.setattr(tts, "_get_pg_pool", lambda s: fp)
        tts._release_conn("agentX", fake_conn)
        # tried normal putconn, then putconn(close=True) to free the slot, then
        # finally a hard close so nothing leaks.
        assert fp.calls == [False, True]
        assert closed["n"] == 1


class TestPoolQueueing:
    """The semaphore gives the pool QUEUEING semantics: excess callers wait for
    a free slot (up to the timeout) instead of getting 'pool exhausted'."""

    def _fake_pool(self):
        return types.SimpleNamespace(
            getconn=lambda: types.SimpleNamespace(autocommit=False),
            putconn=lambda c, close=False: None,
        )

    def test_acquire_times_out_when_all_slots_held(self, monkeypatch):
        tts._PG_POOLS.clear()
        tts._PG_POOL_SEMAS.clear()
        monkeypatch.setenv("SQL_POOL_ENABLED", "1")
        monkeypatch.setenv("SQL_POOL_MAX", "1")
        monkeypatch.setenv("SQL_POOL_ACQUIRE_TIMEOUT_S", "1")
        monkeypatch.setattr(tts, "_get_state", lambda name: _state())
        monkeypatch.setattr(tts, "_get_pg_pool", lambda s: self._fake_pool())

        c1 = tts._acquire_conn("agentX")           # takes the only slot
        import pytest
        with pytest.raises(RuntimeError, match="pool busy"):
            tts._acquire_conn("agentX")            # no slot left -> waits, times out

        tts._release_conn("agentX", c1)            # frees the slot
        c2 = tts._acquire_conn("agentX")           # now succeeds
        assert c2 is not None

    def test_release_balances_the_semaphore(self, monkeypatch):
        tts._PG_POOLS.clear()
        tts._PG_POOL_SEMAS.clear()
        monkeypatch.setenv("SQL_POOL_ENABLED", "1")
        monkeypatch.setenv("SQL_POOL_MAX", "2")
        monkeypatch.setattr(tts, "_get_state", lambda name: _state())
        monkeypatch.setattr(tts, "_get_pg_pool", lambda s: self._fake_pool())
        # acquire+release the full capacity several times: if release did not
        # balance acquire, the slots would run out and this would block/raise.
        for _ in range(5):
            a = tts._acquire_conn("agentX")
            b = tts._acquire_conn("agentX")
            tts._release_conn("agentX", a)
            tts._release_conn("agentX", b)

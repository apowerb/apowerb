"""Generated SQL runs in a read-only transaction with a statement timeout."""

from __future__ import annotations

import pytest

from apowerb.sqlgen.readonly import read_only_transaction, statement_timeout_s


class FakeCursor:
    def __init__(self, log, fail_on=()):
        self.log = log
        self.fail_on = fail_on

    def execute(self, sql, params=None):
        if any(sql.startswith(prefix) for prefix in self.fail_on):
            self.log.append(("failed", sql))
            raise RuntimeError(f"unknown system variable in {sql}")
        self.log.append(("execute", sql, params))


class FakeConn:
    def __init__(self, autocommit):
        self.autocommit = autocommit
        self.log = []

    def rollback(self):
        self.log.append(("rollback", self.autocommit))


class TestPostgres:
    @pytest.mark.parametrize("pooled_autocommit", [True, False])
    def test_read_only_then_timeout_then_query_then_rollback(self, pooled_autocommit):
        conn = FakeConn(autocommit=pooled_autocommit)
        cur = FakeCursor(conn.log)
        with read_only_transaction(conn, cur, "postgresql", timeout_s=30):
            assert conn.autocommit is False  # a real transaction is open
            cur.execute("SELECT 1")
        executed = [e[1] for e in conn.log if e[0] == "execute"]
        assert executed == [
            "SET TRANSACTION READ ONLY",
            "SET LOCAL statement_timeout = %s",
            "SELECT 1",
        ]
        assert ("execute", "SET LOCAL statement_timeout = %s", (30000,)) in conn.log
        assert conn.log[-1][0] == "rollback"
        assert conn.autocommit is pooled_autocommit  # pooled connection restored

    def test_rolls_back_and_restores_when_the_query_fails(self):
        conn = FakeConn(autocommit=True)
        cur = FakeCursor(conn.log)
        with pytest.raises(ValueError):
            with read_only_transaction(conn, cur, "postgresql", timeout_s=5):
                raise ValueError("boom")
        assert conn.log[-1][0] == "rollback"
        assert conn.autocommit is True

    def test_a_dead_connection_does_not_hide_the_original_error(self):
        class DeadConn(FakeConn):
            def rollback(self):
                raise ConnectionError("connection already closed")

        conn = DeadConn(autocommit=True)
        cur = FakeCursor(conn.log)
        with pytest.raises(ValueError, match="original"):
            with read_only_transaction(conn, cur, "postgresql", timeout_s=5):
                raise ValueError("original")
        assert conn.autocommit is True

    def test_no_timeout_when_disabled(self):
        conn = FakeConn(autocommit=False)
        cur = FakeCursor(conn.log)
        with read_only_transaction(conn, cur, "postgresql", timeout_s=0):
            pass
        executed = [e[1] for e in conn.log if e[0] == "execute"]
        assert executed == ["SET TRANSACTION READ ONLY"]


class TestMysql:
    def test_timeout_then_read_only_transaction_then_rollback(self):
        conn = FakeConn(autocommit=False)
        cur = FakeCursor(conn.log)
        with read_only_transaction(conn, cur, "mysql", timeout_s=30):
            cur.execute("SELECT 1")
        executed = [e[1] for e in conn.log if e[0] == "execute"]
        assert executed == [
            "SET SESSION max_execution_time = %s",
            "START TRANSACTION READ ONLY",
            "SELECT 1",
        ]
        assert ("execute", "SET SESSION max_execution_time = %s", (30000,)) in conn.log
        assert conn.log[-1][0] == "rollback"

    def test_falls_back_to_mariadb_timeout(self):
        conn = FakeConn(autocommit=False)
        cur = FakeCursor(conn.log, fail_on=("SET SESSION max_execution_time",))
        with read_only_transaction(conn, cur, "mysql", timeout_s=30):
            pass
        executed = [e[1] for e in conn.log if e[0] == "execute"]
        assert executed == [
            "SET SESSION max_statement_time = %s",
            "START TRANSACTION READ ONLY",
        ]
        assert ("execute", "SET SESSION max_statement_time = %s", (30,)) in conn.log

    def test_runs_without_any_timeout_variable(self):
        conn = FakeConn(autocommit=False)
        cur = FakeCursor(
            conn.log,
            fail_on=(
                "SET SESSION max_execution_time",
                "SET SESSION max_statement_time",
            ),
        )
        with read_only_transaction(conn, cur, "mysql", timeout_s=30):
            pass
        executed = [e[1] for e in conn.log if e[0] == "execute"]
        assert executed == ["START TRANSACTION READ ONLY"]
        assert conn.log[-1][0] == "rollback"


class TestTimeoutSetting:
    def test_defaults_to_30_seconds(self, monkeypatch):
        monkeypatch.delenv("SQL_STATEMENT_TIMEOUT_S", raising=False)
        assert statement_timeout_s() == 30

    def test_reads_the_environment(self, monkeypatch):
        monkeypatch.setenv("SQL_STATEMENT_TIMEOUT_S", "120")
        assert statement_timeout_s() == 120

    @pytest.mark.parametrize("raw", ["abc", "-5", ""])
    def test_invalid_values_fall_back_to_the_default(self, monkeypatch, raw):
        monkeypatch.setenv("SQL_STATEMENT_TIMEOUT_S", raw)
        assert statement_timeout_s() == 30

    def test_zero_disables_it(self, monkeypatch):
        monkeypatch.setenv("SQL_STATEMENT_TIMEOUT_S", "0")
        assert statement_timeout_s() == 0


class TestAgentExecuteSqlIsReadOnly:
    """The Text-to-SQL execution path opens the read-only transaction."""

    def test_query_runs_inside_the_read_only_transaction(self, monkeypatch):
        import types

        from apowerb.tools_store.portfolio import text_to_sql as tts

        conn = FakeConn(autocommit=True)
        cur = FakeCursor(conn.log)
        cur.fetchall = lambda: [{"n": 1}]
        cur.close = lambda: None
        state = types.SimpleNamespace(db_type="postgresql", db_schema="public")
        monkeypatch.setattr(tts, "_get_state", lambda name: state)
        monkeypatch.setattr(tts, "_acquire_conn", lambda name: conn)
        monkeypatch.setattr(tts, "_release_conn", lambda name, c: None)
        monkeypatch.setattr(tts, "_agent_cursor", lambda c, t: cur)

        rows = tts._agent_execute_sql("agentX", "SELECT 1 AS n")

        assert rows == [{"n": 1}]
        executed = [e[1] for e in conn.log if e[0] == "execute"]
        assert executed[0] == "SET TRANSACTION READ ONLY"
        assert executed[-1] == "SELECT 1 AS n LIMIT 100"
        assert conn.log[-1][0] == "rollback"
        assert conn.autocommit is True


class TestDbToRagIsReadOnly:
    """Loading a query into RAG runs it read-only and names a timeout plainly."""

    def _run(self, monkeypatch, raise_on_query=None):
        from apowerb.tools_store.portfolio import db_to_rag

        conn = FakeConn(autocommit=False)
        cur = FakeCursor(conn.log)

        def execute(sql, params=None):
            conn.log.append(("execute", sql, params))
            if raise_on_query and sql.startswith("SELECT"):
                raise raise_on_query

        cur.execute = execute
        cur.fetchall = lambda: []
        cur.close = lambda: None
        cur.description = None
        conn.cursor = lambda cursor_factory=None: cur
        conn.close = lambda: None
        monkeypatch.setattr(db_to_rag, "_get_connection", lambda: conn)
        result = db_to_rag.tool_load_from_db_and_index("SELECT id FROM orders", "kb")
        return conn, result

    def test_query_runs_inside_the_read_only_transaction(self, monkeypatch):
        conn, result = self._run(monkeypatch)
        executed = [e[1] for e in conn.log if e[0] == "execute"]
        assert executed[0] == "SET TRANSACTION READ ONLY"
        assert any(sql.startswith("SELECT id FROM orders") for sql in executed)
        assert conn.log[-1][0] == "rollback"
        assert result["status"] == "empty"

    def test_a_timeout_is_reported_as_a_timeout(self, monkeypatch):
        from psycopg2 import errors

        _, result = self._run(
            monkeypatch, raise_on_query=errors.QueryCanceled("canceling")
        )
        assert result["status"] == "error"
        assert "SQL_STATEMENT_TIMEOUT_S" in result["message"]

    def test_a_write_is_reported_as_refused(self, monkeypatch):
        from psycopg2 import errors

        _, result = self._run(
            monkeypatch, raise_on_query=errors.ReadOnlySqlTransaction("read-only")
        )
        assert result["status"] == "error"
        assert result["message"].startswith("The database refused a write")

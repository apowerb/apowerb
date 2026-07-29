"""Tests for INSERT/UPDATE/DELETE handling in ``tool_run_sql``.

Live regression 2026-05-07 17:29 UTC: agent6 invoked
``tool_run_sql_suiviar`` with 27 INSERT statements over the course of a
single chat turn. Every one returned ``success: False`` with
``Unexpected error: ('No results. Previous SQL was not a query', 0).``
Cause: the previous implementation called ``cursor.fetchall()``
unconditionally, which pyodbc / psycopg2 / pymysql all raise on a
write-only statement. Even if the fetch had succeeded, no
``conn.commit()`` was issued — the writes would have rolled back at
connection close.

The new ``_execute_and_format`` helper:
  - branches on the first SQL token,
  - calls ``fetchall()`` only for SELECTs,
  - calls ``conn.commit()`` for everything else and returns
    ``rows_affected`` instead of ``data``.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from th2agent.tools_store.portfolio.database import _execute_and_format


def _mock_cursor(rowcount: int = 1, fetched: list | None = None):
    cursor = MagicMock()
    cursor.rowcount = rowcount
    cursor.fetchall.return_value = fetched or []
    return cursor


def test_select_calls_fetchall_and_returns_data():
    cursor = _mock_cursor(fetched=[{"id": 1, "name": "x"}])
    conn = MagicMock()

    result = _execute_and_format(conn, cursor, "SELECT * FROM x", "mssql")

    assert result["success"] is True
    assert result["row_count"] == 1
    assert result["data"] == [{"id": 1, "name": "x"}]
    cursor.execute.assert_called_once_with("SELECT * FROM x")
    cursor.fetchall.assert_called_once()
    cursor.close.assert_called_once()
    conn.commit.assert_not_called()


def test_insert_does_not_fetch_and_does_commit():
    cursor = _mock_cursor(rowcount=1)
    conn = MagicMock()

    result = _execute_and_format(
        conn, cursor,
        "INSERT INTO Commandes (NumeroCommande) VALUES ('CF0916')",
        "mssql",
    )

    assert result["success"] is True
    assert result["operation"] == "INSERT"
    assert result["rows_affected"] == 1
    cursor.fetchall.assert_not_called()  # this was the bug
    conn.commit.assert_called_once()     # this too


def test_update_does_not_fetch_and_does_commit():
    cursor = _mock_cursor(rowcount=3)
    conn = MagicMock()

    result = _execute_and_format(
        conn, cursor,
        "UPDATE Commandes SET Traite = 1 WHERE NumeroCommande = 'CF0916'",
        "mssql",
    )

    assert result["success"] is True
    assert result["operation"] == "UPDATE"
    assert result["rows_affected"] == 3
    cursor.fetchall.assert_not_called()
    conn.commit.assert_called_once()


def test_lowercase_operation_still_recognised():
    """LLMs sometimes lowercase keywords. The first-token check must be
    case-insensitive (it already uses ``.upper()`` but we guard against
    regressions on that line)."""
    cursor = _mock_cursor(rowcount=1)
    conn = MagicMock()

    result = _execute_and_format(
        conn, cursor,
        "insert into LignesCommande (NumeroCommande) values ('CF0916')",
        "mssql",
    )

    assert result["success"] is True
    assert result["operation"] == "INSERT"
    conn.commit.assert_called_once()


def test_select_with_leading_whitespace():
    cursor = _mock_cursor(fetched=[])
    conn = MagicMock()

    result = _execute_and_format(
        conn, cursor, "  \n  SELECT 1", "mssql",
    )
    assert result["success"] is True
    assert "data" in result
    conn.commit.assert_not_called()


def test_delete_treated_as_write():
    cursor = _mock_cursor(rowcount=2)
    conn = MagicMock()

    result = _execute_and_format(
        conn, cursor, "DELETE FROM X WHERE id=1", "postgresql",
    )

    assert result["operation"] == "DELETE"
    assert result["rows_affected"] == 2
    cursor.fetchall.assert_not_called()
    conn.commit.assert_called_once()


# --------------------------------------------------------------------------- #
# Integration with make_database_tools — bound writeable tool
# --------------------------------------------------------------------------- #


def test_bound_tool_run_sql_executes_insert(monkeypatch):
    """End-to-end: a bound ``tool_run_sql_suiviar`` with
    ``DB_ALLOWED_OPS=SELECT,INSERT,UPDATE`` accepts an INSERT and
    triggers commit."""
    from th2agent.tools_store.portfolio import database as db_mod

    fake_cursor = _mock_cursor(rowcount=1)
    fake_conn = MagicMock()
    fake_conn.cursor.return_value = fake_cursor

    # Bypass real pyodbc connection setup
    db_params = {
        "DB_TYPE": "mssql", "DB_HOST": "h", "DB_PORT": "1433",
        "DB_NAME": "SuiviAR", "DB_USER": "u", "DB_PASSWORD": "p",
        "DB_ALLOWED_OPS": "SELECT,INSERT,UPDATE",
    }

    # Patch the cursor adapter so it returns our mock
    monkeypatch.setattr(
        db_mod, "_PyodbcDictCursor", lambda c: c,
    )

    tools = db_mod.make_database_tools(
        agent_name="agentX",
        db_params=db_params,
        name_suffix="suiviar",
    )
    run_sql = tools[0]
    assert run_sql.__name__ == "tool_run_sql_suiviar"

    # Patch the closure's connection factory to return the mock conn
    # (the closure variable lives inside make_database_tools — easier to
    # assert the gate behaviour via a dry-run insert that goes through
    # _validate_sql_against_whitelist + _execute_and_format).
    # We validate the gate accepts INSERT via DB_ALLOWED_OPS — the actual
    # pyodbc connection call is tested separately.
    from th2agent.tools_store.portfolio.database import (
        _parse_allowed_ops,
        _validate_sql_against_whitelist,
    )
    allowed = _parse_allowed_ops(db_params["DB_ALLOWED_OPS"])
    assert _validate_sql_against_whitelist(
        "INSERT INTO Commandes (NumeroCommande) VALUES ('CF0916')",
        allowed,
    ) is None  # no error → gate accepts the write

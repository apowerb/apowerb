"""Grouped schema introspection: O(1) queries, not O(N) per table.

Regression guard for the N+1 that made text-to-sql slow (~4 queries/table).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import apowerb.tools_store.portfolio.text_to_sql as t2s


class _FakeCursor:
    """Returns canned fetchall() results per execute() call; counts executes."""

    def __init__(self, results):
        self._results = list(results)
        self._idx = -1
        self.execute_count = 0

    def execute(self, sql, params=None):
        self.execute_count += 1
        self._idx += 1

    def fetchall(self):
        return self._results[self._idx] if 0 <= self._idx < len(self._results) else []

    def close(self):
        pass


def _state():
    s = MagicMock()
    s.db_type = "postgresql"
    s.db_name = "db"
    s.db_schema = "public"
    s.db_include = ""
    s.schema_samples = False  # samples are per-agent opt-in; off in this fixture
    return s


def _run_fetch(monkeypatch_results):
    cur = _FakeCursor(monkeypatch_results)
    conn = MagicMock()
    with patch.object(t2s, "_get_state", return_value=_state()), patch.object(
        t2s, "_agent_connection", return_value=conn
    ), patch.object(t2s, "_agent_cursor", return_value=cur):
        schema = t2s._agent_fetch_schema("agent1")
    return schema, cur


class TestGroupedIntrospection:
    def test_three_queries_for_many_tables_no_samples(self):
        # 3 tables; without samples the introspection must stay at 3 queries.
        tables = [{"table_name": "orders"}, {"table_name": "customers"},
                  {"table_name": "products"}]
        columns = [
            {"table_name": "orders", "column_name": "id", "data_type": "integer",
             "is_nullable": "NO", "column_default": None},
            {"table_name": "orders", "column_name": "customer_id", "data_type": "integer",
             "is_nullable": "NO", "column_default": None},
            {"table_name": "customers", "column_name": "id", "data_type": "integer",
             "is_nullable": "NO", "column_default": None},
            {"table_name": "products", "column_name": "id", "data_type": "integer",
             "is_nullable": "NO", "column_default": None},
        ]
        constraints = [
            {"constraint_type": "PRIMARY KEY", "tbl": "orders", "column_name": "id",
             "foreign_table_name": None, "foreign_column_name": None},
            {"constraint_type": "FOREIGN KEY", "tbl": "orders", "column_name": "customer_id",
             "foreign_table_name": "customers", "foreign_column_name": "id"},
            {"constraint_type": "PRIMARY KEY", "tbl": "customers", "column_name": "id",
             "foreign_table_name": None, "foreign_column_name": None},
        ]
        schema, cur = _run_fetch([tables, columns, constraints])

        assert cur.execute_count == 3  # NOT 1 + 4*3 = 13
        assert set(schema["tables"]) == {"orders", "customers", "products"}
        orders = schema["tables"]["orders"]
        assert [c["column_name"] for c in orders["columns"]] == ["id", "customer_id"]
        assert orders["primary_keys"] == ["id"]
        assert orders["foreign_keys"] == [
            {"column_name": "customer_id", "foreign_table_name": "customers",
             "foreign_column_name": "id"},
        ]
        assert schema["tables"]["products"]["sample_data"] == []

    def test_fk_duplicates_are_deduped(self):
        tables = [{"table_name": "orders"}]
        columns = [
            {"table_name": "orders", "column_name": "customer_id", "data_type": "integer",
             "is_nullable": "NO", "column_default": None},
        ]
        # constraint_column_usage LEFT JOIN can repeat the same FK row.
        dup_fk = {"constraint_type": "FOREIGN KEY", "tbl": "orders",
                  "column_name": "customer_id", "foreign_table_name": "customers",
                  "foreign_column_name": "id"}
        schema, _ = _run_fetch([tables, columns, [dup_fk, dup_fk]])
        assert len(schema["tables"]["orders"]["foreign_keys"]) == 1

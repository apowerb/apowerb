"""Unit tests for the pure sqlgen building blocks."""

from __future__ import annotations

import pytest

from apowerb.sqlgen.generator import extract_sql
from apowerb.sqlgen.safety import validate_sql_safety
from apowerb.sqlgen.schema_format import build_schema_prompt, compact_table_line
from apowerb.sqlgen.table_selection import score_and_order_tables


def _schema():
    return {
        "schema": "public",
        "db_type": "postgresql",
        "tables": {
            "orders": {
                "columns": [
                    {"column_name": "id", "data_type": "integer"},
                    {"column_name": "customer_id", "data_type": "integer"},
                    {"column_name": "total", "data_type": "numeric"},
                    {"column_name": "created_at", "data_type": "timestamp"},
                ],
                "primary_keys": ["id"],
                "foreign_keys": [
                    {
                        "column_name": "customer_id",
                        "foreign_table_name": "customers",
                        "foreign_column_name": "id",
                    },
                ],
                "sample_data": [{"id": 1, "customer_id": 2, "total": 10}],
            },
            "customers": {
                "columns": [
                    {"column_name": "id", "data_type": "integer"},
                    {"column_name": "name", "data_type": "varchar"},
                ],
                "primary_keys": ["id"],
                "foreign_keys": [],
                "sample_data": [{"id": 2, "name": "Acme"}],
            },
            "audit_log": {
                "columns": [
                    {"column_name": "id", "data_type": "integer"},
                    {"column_name": "message", "data_type": "text"},
                ],
                "primary_keys": ["id"],
                "foreign_keys": [],
                "sample_data": [],
            },
        },
    }


class TestCompactTableLine:
    def test_marks_pk_and_fk(self):
        line = compact_table_line("orders", _schema()["tables"]["orders"])
        assert line.startswith("orders(")
        assert "id integer PK" in line
        assert "customer_id integer FK->customers.id" in line
        # Far more compact than the old emoji multi-line format.
        assert "\n" not in line


class TestBuildSchemaPrompt:
    def test_compact_no_samples_by_default(self):
        out = build_schema_prompt(_schema())
        assert "Database schema (schema: public, type: postgresql):" in out
        assert "orders(" in out and "customers(" in out
        # samples off by default
        assert "e.g." not in out

    def test_samples_opt_in_truncated(self):
        out = build_schema_prompt(_schema(), include_samples=True, sample_rows=1)
        assert "e.g." in out

    def test_char_budget_omits_tables_and_lists_them(self):
        # Tiny budget: only the most relevant table fits, the rest are listed.
        out = build_schema_prompt(
            _schema(), question="orders by customer", max_chars=60
        )
        assert "Other tables (call tool_get_database_schema" in out
        # The single most relevant table is always kept.
        assert "orders(" in out

    def test_relevant_tables_pulled_together_via_fk(self):
        order = score_and_order_tables(_schema(), "total orders per customer")
        # orders matches (orders, total, customer_id); customers is FK-boosted;
        # audit_log is irrelevant and ranks last.
        assert order[0] == "orders"
        assert order.index("customers") < order.index("audit_log")


class TestTableSelection:
    def test_no_question_is_alphabetical(self):
        assert score_and_order_tables(_schema()) == ["audit_log", "customers", "orders"]


class TestSafety:
    def test_allows_select(self):
        ok, err = validate_sql_safety("SELECT * FROM orders")
        assert ok and err is None

    def test_rejects_dml_and_multistatement(self):
        assert validate_sql_safety("DELETE FROM orders")[0] is False
        assert validate_sql_safety("SELECT 1; SELECT 2")[0] is False
        assert validate_sql_safety("UPDATE orders SET x=1")[0] is False


class TestSafetyReadOnly:
    """A query that starts with SELECT can still write, lock or stall.

    The gate must refuse what a read-only analytics query never needs:
    writing a result into a table or file, row locks, and the functions
    that sleep, read the server's files or reach other connections.
    """

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * INTO new_orders FROM orders",
            "SELECT id INTO TEMP t FROM orders",
            "SELECT * FROM orders INTO OUTFILE '/tmp/orders.csv'",
            "WITH a AS (SELECT 1 AS n) SELECT n INTO t FROM a",
            "SELECT pg_sleep(10)",
            "SELECT pg_sleep_for('5 minutes')",
            "SELECT SLEEP(10)",
            "SELECT BENCHMARK(1000000000, MD5('x'))",
            "SELECT * FROM orders WAITFOR DELAY '00:00:10'",
            "SELECT pg_read_file('/etc/passwd')",
            "SELECT lo_export(1, '/tmp/x')",
            "SELECT * FROM dblink('host=evil', 'SELECT 1') AS t(n int)",
            "SELECT pg_terminate_backend(42)",
            "SELECT set_config('search_path', 'other', false)",
            "SELECT * FROM orders FOR UPDATE",
            "SELECT * FROM orders FOR SHARE",
            "SELECT * FROM orders FOR KEY SHARE",
            # Quoting, schema-qualifying or commenting the name changes nothing.
            'SELECT "pg_sleep"(10)',
            'SELECT pg_catalog."pg_sleep"(10)',
            "SELECT pg_catalog.pg_sleep(10)",
            "SELECT Pg_Sleep(10)",
            "SELECT pg_sleep/**/(10)",
            "SELECT pg_sleep -- c\n(10)",
            "SELECT * FROM orders FOR/**/SHARE",
            "SELECT * /* x */ INTO t FROM orders",
            "SELECT lo_unlink(1)",
            "SELECT pg_reload_conf()",
            "SELECT pg_advisory_lock(1)",
            "SELECT nextval('s')",
            # An unterminated literal or comment cannot be read safely.
            "SELECT 'abc",
            "SELECT 1 /* never closed",
            # Backslash escapes (E'...' in Postgres, MySQL strings) make the end
            # of a literal ambiguous, so a call could hide behind them.
            "SELECT E'\\'', pg_sleep(10), 'abc', E'\\''",
            "SELECT 'it\\'s'",
            # Other quoting the reader does not model: Postgres dollar quotes,
            # MySQL backticks, and MySQL comments that MySQL executes.
            "SELECT $$'$$, pg_sleep(10), $$'$$",
            "SELECT $a$'$a$, pg_sleep(10), $a$'$a$",
            "SELECT `'`, SLEEP(1), `'`",
            "SELECT /*! SLEEP(5) */ 1",
            # '#' starts a comment in MySQL but is an operator in Postgres.
            "SELECT 1 #'\n, SLEEP(1) #'",
            "SELECT 1 # 2, pg_sleep(1)",
            # The cost of that rule: jsonb path operators are refused too.
            "SELECT payload #> '{a}' FROM events",
            # A '--' comment also ends at a bare carriage return.
            "SELECT 1 -- x\r, pg_sleep(1)",
            # SQL Server bracket identifiers holding a quote.
            "SELECT [a'] , pg_sleep(1), [b'] FROM t",
            "SELECT [x]]'], pg_sleep(1), [y]]']",
        ],
    )
    def test_rejects_select_that_writes_locks_or_stalls(self, sql):
        ok, err = validate_sql_safety(sql)
        assert ok is False
        assert err

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT sleep_hours, intolerance FROM patients",
            "SELECT * FROM orders WHERE note = 'for sharing'",
            "SELECT customer_id, SUM(total) FROM orders GROUP BY customer_id",
            "WITH t AS (SELECT 1 AS n) SELECT * FROM t",
            # Keywords inside string literals or comments are just text.
            "SELECT 'INTO' AS k",
            "SELECT * FROM orders WHERE note LIKE '%into the cart%'",
            "SELECT * FROM orders WHERE status = 'updated; drop later'",
            "SELECT * FROM orders WHERE note = 'it''s pg_sleep(1)'",
            "SELECT id FROM orders -- copy INTO later\n",
            "/* monthly report */ SELECT id FROM orders",
            "SELECT * FROM orders WHERE ref LIKE '%#%'",
            "SELECT [Order Date], tags[1] FROM orders",
        ],
    )
    def test_still_allows_plain_reads(self, sql):
        ok, err = validate_sql_safety(sql)
        assert ok, err


class TestExtractSql:
    def test_strips_markdown_fence(self):
        assert extract_sql("```sql\nSELECT 1\n```") == "SELECT 1"

    def test_strips_preamble(self):
        assert (
            extract_sql("Here is the query: SELECT id FROM orders;")
            == "SELECT id FROM orders"
        )

    def test_handles_with_cte(self):
        out = extract_sql("```\nWITH t AS (SELECT 1) SELECT * FROM t\n```")
        assert out.startswith("WITH t AS")

    def test_empty(self):
        assert extract_sql("") == ""


class TestSafetyCte:
    """WITH ... SELECT (CTE) must be allowed; data-modifying CTEs must not.

    extract_sql accepts WITH as a statement start (Mistral emits CTEs for
    analytics), so validate_sql_safety must accept it too — otherwise every CTE
    fails the safety gate, triggers the retry, and can return empty SQL.
    """

    def test_allows_with_cte(self):
        ok, err = validate_sql_safety(
            "WITH monthly AS (SELECT 1 AS n) SELECT * FROM monthly"
        )
        assert ok and err is None

    def test_extract_then_validate_cte_end_to_end(self):
        raw = "```sql\nWITH t AS (SELECT 1 AS n) SELECT * FROM t\n```"
        sql = extract_sql(raw)
        ok, err = validate_sql_safety(sql)
        assert ok, err

    def test_rejects_data_modifying_cte(self):
        # WITH ... DELETE is still blocked by the forbidden-keyword scan.
        ok, _ = validate_sql_safety(
            "WITH x AS (SELECT id FROM orders) DELETE FROM orders"
        )
        assert ok is False

    def test_word_starting_with_with_is_not_a_cte(self):
        # 'WITH' must match the keyword (word boundary), not any prefix.
        assert validate_sql_safety("WITHHOLDING FROM orders")[0] is False

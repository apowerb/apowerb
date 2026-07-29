"""Unit tests for the pure sqlgen building blocks."""

from __future__ import annotations

from th2agent.sqlgen.generator import extract_sql
from th2agent.sqlgen.safety import validate_sql_safety
from th2agent.sqlgen.schema_format import build_schema_prompt, compact_table_line
from th2agent.sqlgen.table_selection import score_and_order_tables


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
                    {"column_name": "customer_id",
                     "foreign_table_name": "customers",
                     "foreign_column_name": "id"},
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
        out = build_schema_prompt(_schema(), question="orders by customer",
                                  max_chars=60)
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


class TestExtractSql:
    def test_strips_markdown_fence(self):
        assert extract_sql("```sql\nSELECT 1\n```") == "SELECT 1"

    def test_strips_preamble(self):
        assert extract_sql("Here is the query: SELECT id FROM orders;") == \
            "SELECT id FROM orders"

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

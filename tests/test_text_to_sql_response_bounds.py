"""Model-facing bounds of the text_to_sql tool responses."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import apowerb.tools_store.portfolio.text_to_sql as t2s

_SCHEMA = {
    "schema": "public",
    "tables": {
        "orders": {
            "columns": [
                {"column_name": "id", "data_type": "integer", "is_nullable": "NO", "column_default": "nextval()"},
                {"column_name": "customer_id", "data_type": "integer", "is_nullable": "YES", "column_default": None},
            ],
            "primary_keys": ["id"],
            "foreign_keys": [
                {"column_name": "customer_id", "foreign_table_name": "customers", "foreign_column_name": "id"}
            ],
            "sample_data": [],
        },
        "customers": {
            "columns": [{"column_name": "id", "data_type": "integer", "is_nullable": "NO", "column_default": None}],
            "primary_keys": ["id"],
            "foreign_keys": [],
            "sample_data": [],
        },
    },
}


def _schema_call(**kwargs):
    state = MagicMock()
    state.db_name = "db"
    with patch.object(t2s, "_get_state", return_value=state), patch.object(
        t2s, "_agent_cached_schema", return_value=_SCHEMA
    ):
        return t2s._tool_get_database_schema_impl("agent1", **kwargs)


def test_schema_is_compact():
    out = _schema_call()
    assert out["tables"]["orders"] == {
        "columns": ["id integer", "customer_id integer"],
        "pk": ["id"],
        "fk": ["customer_id->customers.id"],
    }
    assert "is_nullable" not in str(out)


def test_schema_filter_and_unknown_tables():
    out = _schema_call(tables=["Customers", "nope"])
    assert list(out["tables"]) == ["customers"]
    assert out["unknown_tables"] == ["nope"]
    assert out["table_count"] == 2


def test_schema_cap_lists_omitted_tables(monkeypatch):
    monkeypatch.setattr(t2s, "_SCHEMA_TOOL_MAX_CHARS", 80)
    out = _schema_call()
    assert out["truncated"] is True
    assert out["omitted_tables"] == ["customers"]


def _sql_call(rows, max_rows):
    with patch.object(t2s, "_agent_cached_schema", return_value=_SCHEMA), patch.object(
        t2s, "_agent_generate_sql", return_value="SELECT * FROM orders"
    ), patch.object(t2s, "_agent_execute_sql", return_value=rows) as ex:
        out = t2s._tool_text_to_sql_impl("agent1", "q", max_rows=max_rows)
    return out, ex


def test_text_to_sql_caps_rows_and_flags_truncation():
    rows = [{"i": i} for i in range(300)]
    out, ex = _sql_call(rows, max_rows=5000)
    assert ex.call_args.kwargs["limit"] == 201
    assert out["row_count"] == 200 and len(out["data"]) == 200
    assert out["truncated"] is True
    assert "schema" not in out and "question" not in out


def test_text_to_sql_not_truncated_when_fits():
    out, _ = _sql_call([{"i": 1}, {"i": 2}], max_rows=10)
    assert "truncated" not in out
    assert out["row_count"] == 2


def test_business_analyst_bounds_and_step_rows():
    step = MagicMock(sub_question="s", sql="SELECT 1", rows=[{"i": i} for i in range(40)], error=None, row_count=40)
    result = MagicMock(ok=True, steps=[step], narrative="n", findings=[], recommendations=[], chart=None)
    with patch.object(t2s, "_agent_cached_schema", return_value=_SCHEMA), patch.object(
        t2s, "_get_state", return_value=MagicMock(schema_samples=False)
    ), patch.object(t2s, "build_schema_prompt", return_value="p"), patch(
        "apowerb.sqlgen.analyst.run_investigation", return_value=result
    ) as run:
        out = t2s._tool_business_analyst_impl("agent1", "q", max_steps=99, max_rows=999)
    assert run.call_args.kwargs["max_steps"] == 6
    assert out["steps"][0]["row_count"] == 40
    assert len(out["steps"][0]["data"]) == 20

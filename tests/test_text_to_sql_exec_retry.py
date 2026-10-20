"""tool_text_to_sql repairs a query the database rejects, inside the tool.

Before, a wrong column or join came back to the agent as an error, and the
template's error rule (schema, then retry, up to twice) cost two to four more
calls of the main model, each carrying the whole prompt and every tool
declaration. One corrective regeneration with the database error, inside the
tool, costs one small call of the SQL generator instead.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import psycopg2

import apowerb.tools_store.portfolio.text_to_sql as t2s

_SCHEMA = {
    "schema": "public",
    "db_type": "postgresql",
    "tables": {
        "orders": {
            "columns": [{"column_name": "id", "data_type": "integer"},
                        {"column_name": "amount", "data_type": "numeric"}],
            "primary_keys": ["id"],
            "foreign_keys": [],
            "sample_data": [],
        }
    },
}


def _resp(content):
    r = MagicMock()
    r.choices = [MagicMock()]
    r.choices[0].message.content = content
    return r


def _state():
    s = MagicMock()
    s.model, s.api_base, s.api_key = "m", "http://endpoint", "k"
    s.agent_name, s.db_type, s.schema_samples = "agent1", "postgresql", False
    return s


def _run(sqls, execute):
    with patch.object(t2s, "_get_state", return_value=_state()), patch.object(
        t2s, "_agent_cached_schema", return_value=_SCHEMA
    ), patch.object(t2s, "_record_llm_usage"), patch.object(
        t2s.litellm, "completion", side_effect=[_resp(s) for s in sqls]
    ) as comp, patch.object(t2s, "_agent_execute_sql", side_effect=execute) as ex:
        out = t2s._tool_text_to_sql_impl("agent1", "total amount")
    return out, comp, ex


def test_database_error_is_repaired_inside_the_tool():
    bad, good = "SELECT SUM(total) FROM public.orders", "SELECT SUM(amount) AS total FROM public.orders"
    err = psycopg2.errors.UndefinedColumn('column "total" does not exist')
    out, comp, ex = _run([bad, good], [err, [{"total": 42}]])

    assert out["success"] is True
    assert out["data"] == [{"total": 42}]
    assert out["sql_query"] == good
    assert comp.call_count == 2 and ex.call_count == 2
    # The regeneration sees the rejected SQL and the database's own message.
    retry = comp.call_args_list[1].kwargs["messages"]
    assert retry[-2] == {"role": "assistant", "content": bad}
    assert 'column "total" does not exist' in retry[-1]["content"]


def test_second_failure_returns_the_real_error_once():
    errs = [psycopg2.errors.UndefinedColumn("no col a"), psycopg2.errors.UndefinedColumn("no col b")]
    out, comp, ex = _run(["SELECT a FROM public.orders", "SELECT b FROM public.orders"], errs)

    assert out["success"] is False
    assert "no col b" in out["error"]
    assert out["sql_query"] == "SELECT b FROM public.orders"
    assert comp.call_count == 2 and ex.call_count == 2


def test_permission_errors_are_not_retried():
    out, comp, ex = _run(["SELECT amount FROM public.orders"],
                         [psycopg2.errors.InsufficientPrivilege("permission denied for table orders")])

    assert out["success"] is False
    assert comp.call_count == 1 and ex.call_count == 1


def test_connection_errors_are_not_retried():
    out, comp, ex = _run(["SELECT amount FROM public.orders"],
                         [psycopg2.OperationalError("could not connect to server")])

    assert out["success"] is False
    assert "could not connect" in out["error"]
    assert comp.call_count == 1 and ex.call_count == 1

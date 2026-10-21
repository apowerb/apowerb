"""tool_chart_from_question: query + saved chart in ONE tool call.

An inline chart used to take three tool calls in a row (tool_text_to_sql,
tool_create_chart, embed_chart), each one a full model call. The composite
does the first two, so the agent goes straight to embed_chart.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from apowerb.core.agent_helpers.chart_from_question_tool import (
    bind_chart_from_question,
)


def _tools(t2s_result, chart_result):
    def tool_text_to_sql(question, max_rows=100, include_sql=True, tool_context=None):
        return t2s_result

    def tool_create_chart(name, title, chart_type, query):
        return chart_result

    t2s = MagicMock(side_effect=tool_text_to_sql, __name__="tool_text_to_sql")
    chart = MagicMock(side_effect=tool_create_chart, __name__="tool_create_chart")
    return t2s, chart


def _bound(t2s, chart, enabled=True):
    funcs = bind_chart_from_question("agent1", [t2s, chart], enabled)
    return {f.__name__: f for f in funcs}


_ROWS = {"success": True, "row_count": 2, "data": [{"carrier": "DHL", "n": 3}, {"carrier": "UPS", "n": 5}],
         "sql_query": "SELECT carrier, COUNT(*) AS n FROM public.parcels GROUP BY carrier"}
_CHART = {"success": True, "chart_id": "c-1", "name": "parcels-by-carrier", "title": "Parcels by carrier",
          "chart_type": "bar"}


def test_queries_then_saves_the_chart_with_the_generated_sql():
    t2s, chart = _tools(_ROWS, _CHART)
    out = _bound(t2s, chart)["tool_chart_from_question"](
        question="parcels per carrier", chart_type="bar", title="Parcels by carrier")

    assert out["success"] is True
    assert out["chart_id"] == "c-1"
    assert out["data"] == _ROWS["data"] and out["sql_query"] == _ROWS["sql_query"]
    assert "embed_chart" in out["message"]
    kw = chart.call_args.kwargs
    assert kw["query"] == _ROWS["sql_query"]
    assert kw["chart_type"] == "bar" and kw["title"] == "Parcels by carrier"
    assert kw["name"] == "parcels-by-carrier"


def test_query_failure_saves_no_chart():
    t2s, chart = _tools({"success": False, "error": "boom", "sql_query": "SELECT x"}, _CHART)
    out = _bound(t2s, chart)["tool_chart_from_question"](question="q", chart_type="bar", title="T")

    assert out["success"] is False and out["error"] == "boom"
    chart.assert_not_called()


def test_chart_failure_is_reported_with_the_rows():
    t2s, chart = _tools(_ROWS, {"success": False, "error": "Invalid chart_type 'barz'"})
    out = _bound(t2s, chart)["tool_chart_from_question"](question="q", chart_type="barz", title="T")

    assert out["success"] is False
    assert "Invalid chart_type" in out["error"]
    assert out["sql_query"] == _ROWS["sql_query"] and "chart_id" not in out


def test_only_added_when_enabled_and_both_tools_exist():
    t2s, chart = _tools(_ROWS, _CHART)
    assert "tool_chart_from_question" not in _bound(t2s, chart, enabled=False)
    assert bind_chart_from_question("agent1", [t2s], True) == [t2s]
    assert len(bind_chart_from_question("agent1", [t2s, chart], True)) == 3


def test_declaration_hides_tool_context():
    from google.adk.tools import FunctionTool

    t2s, chart = _tools(_ROWS, _CHART)
    decl = FunctionTool(_bound(t2s, chart)["tool_chart_from_question"])._get_declaration()
    params = decl.parameters.properties if decl.parameters else decl.parameters_json_schema["properties"]
    assert set(params) == {"question", "chart_type", "title"}


def test_chart_name_keeps_accented_letters():
    from apowerb.core.agent_helpers.chart_from_question_tool import _chart_name

    assert _chart_name("Colis par département") == "colis-par-departement"
    assert _chart_name("") == "chart"


def test_guardrails_blocking_a_building_block_disables_the_composite():
    import json

    from apowerb.core.agent_helpers.agent_utils import _guardrails_block

    names = ("tool_text_to_sql", "tool_create_chart")
    assert _guardrails_block({"guardrails_config": json.dumps({"blocked_tools": ["Tool_Create_Chart"]})}, names)
    assert not _guardrails_block({"guardrails_config": json.dumps({"blocked_tools": ["tool_run_sql"]})}, names)
    assert not _guardrails_block({"guardrails_config": None}, names)
    assert not _guardrails_block({"guardrails_config": "not json"}, names)

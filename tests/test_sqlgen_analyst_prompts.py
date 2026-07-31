"""Unit tests for the pure Business-Analyst prompt/parse helpers."""

from __future__ import annotations

from apowerb.sqlgen.analyst import InvestigationStep
from apowerb.sqlgen.analyst_prompts import (
    build_interpreter_messages,
    build_planner_messages,
    parse_interpreter_response,
    parse_planner_response,
    steps_digest,
)


def _step(sub="q", sql="SELECT 1", rows=None, error=None):
    return InvestigationStep(sub_question=sub, sql=sql, rows=rows or [], error=error)


class TestStepsDigest:
    def test_empty(self):
        assert steps_digest([]) == "(no queries run yet)"

    def test_shows_error(self):
        out = steps_digest([_step(error="db down")])
        assert "ERROR: db down" in out

    def test_shows_rows_and_count(self):
        out = steps_digest([_step(rows=[{"total": 10}, {"total": 20}])])
        assert "RESULT (2 rows)" in out
        assert "total=10" in out

    def test_truncates_preview_and_reports_more(self):
        rows = [{"n": i} for i in range(9)]
        out = steps_digest([_step(rows=rows)])
        assert "+4 more rows" in out          # 9 rows, preview 5

    def test_zero_rows(self):
        out = steps_digest([_step(rows=[])])
        assert "0 rows" in out


class TestPlannerMessages:
    def test_carries_question_schema_and_history(self):
        msgs = build_planner_messages(
            "revenue per month?", "SCHEMA_X",
            [_step(sub="totals", rows=[{"total": 5}])], max_steps=4)
        assert msgs[0]["role"] == "system"
        joined = msgs[0]["content"] + msgs[1]["content"]
        assert "revenue per month?" in joined
        assert "SCHEMA_X" in joined
        assert "totals" in joined
        assert "4 queries" in joined or "more than 4" in joined


class TestInterpreterMessages:
    def test_carries_question_and_evidence(self):
        msgs = build_interpreter_messages("why drop?", [_step(rows=[{"x": 1}])])
        joined = msgs[0]["content"] + msgs[1]["content"]
        assert "why drop?" in joined
        assert "x=1" in joined


class TestParsePlanner:
    def test_plain_json(self):
        out = parse_planner_response('{"done": false, "sub_question": "a", "sql": "SELECT 1"}')
        assert out == {"done": False, "sub_question": "a", "sql": "SELECT 1"}

    def test_fenced_json(self):
        out = parse_planner_response('```json\n{"done": false, "sql": "SELECT 2"}\n```')
        assert out["done"] is False and out["sql"] == "SELECT 2"

    def test_json_with_prose_around(self):
        out = parse_planner_response('Sure! {"done": false, "sql": "SELECT 3"} hope that helps')
        assert out["sql"] == "SELECT 3"

    def test_done_true(self):
        assert parse_planner_response('{"done": true}') == {"done": True}

    def test_garbage_fails_safe_to_done(self):
        assert parse_planner_response("I cannot help with that") == {"done": True}

    def test_missing_sql_fails_safe_to_done(self):
        assert parse_planner_response('{"done": false, "sub_question": "x"}') == {"done": True}


class TestParseInterpreter:
    def test_full_object(self):
        raw = ('{"narrative": "Up 10%.", "findings": ["trend"], '
               '"recommendations": ["scale"], '
               '"chart": {"chart_type": "line", "x": "m", "y": "t", "title": "T"}}')
        out = parse_interpreter_response(raw)
        assert out["narrative"] == "Up 10%."
        assert out["findings"] == ["trend"]
        assert out["chart"]["chart_type"] == "line"

    def test_non_dict_chart_becomes_null(self):
        out = parse_interpreter_response('{"narrative": "x", "chart": "bar"}')
        assert out["chart"] is None

    def test_garbage_degrades_to_plain_narrative(self):
        out = parse_interpreter_response("Revenue clearly went up this quarter.")
        assert out["narrative"] == "Revenue clearly went up this quarter."
        assert out["findings"] == [] and out["chart"] is None

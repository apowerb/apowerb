"""Unit tests for the pure Business-Analyst investigation/interpretation loop.

These tests inject fake planner/executor/interpreter callables so the loop is
exercised with NO database and NO LLM — same purity contract as test_sqlgen.py.
"""

from __future__ import annotations

from apowerb.sqlgen.analyst import (
    AnalysisResult,
    InvestigationStep,
    run_investigation,
)


def _schema():
    return {
        "schema": "public",
        "db_type": "postgresql",
        "tables": {"orders": {"columns": [{"column_name": "total",
                                           "data_type": "numeric"}]}},
    }


def _planner(script):
    """Return a plan_next that yields scripted dicts then {done:True}."""
    calls = {"i": 0}
    seen = {"steps": []}

    def plan_next(question, schema_info, steps):
        seen["steps"] = steps
        i = calls["i"]
        calls["i"] += 1
        if i < len(script):
            return script[i]
        return {"done": True}

    plan_next.calls = calls
    plan_next.seen = seen
    return plan_next


def _interp(out=None):
    captured = {}

    def interpret(question, steps):
        captured["steps"] = steps
        return out or {
            "narrative": "n", "findings": ["f"],
            "recommendations": ["r"], "chart": None,
        }

    interpret.captured = captured
    return interpret


class TestInvestigationLoop:
    def test_stops_on_done(self):
        plan = _planner([
            {"sub_question": "q1", "sql": "SELECT total FROM orders"},
            {"done": True},
        ])
        res = run_investigation(
            "revenue?", _schema(),
            plan_next=plan, execute=lambda sql: [{"total": 10}],
            interpret=_interp(),
        )
        assert isinstance(res, AnalysisResult)
        assert len(res.steps) == 1
        assert res.steps[0].row_count == 1
        assert res.steps[0].error is None

    def test_respects_max_steps(self):
        # Planner never says done -> loop must cap at max_steps.
        plan = _planner([{"sub_question": "q", "sql": "SELECT total FROM orders"}] * 10)
        res = run_investigation(
            "q", _schema(), plan_next=plan,
            execute=lambda sql: [{"total": 1}], interpret=_interp(),
            max_steps=3,
        )
        assert len(res.steps) == 3

    def test_unsafe_sql_is_never_executed(self):
        executed = []
        plan = _planner([
            {"sub_question": "drop", "sql": "DROP TABLE orders"},
            {"done": True},
        ])
        res = run_investigation(
            "q", _schema(), plan_next=plan,
            execute=lambda sql: executed.append(sql) or [],
            interpret=_interp(),
        )
        assert executed == []                     # executor never called
        assert res.steps[0].error is not None     # recorded as a failed step
        assert res.steps[0].row_count == 0

    def test_extract_sql_applied_to_planner_output(self):
        executed = []
        plan = _planner([
            {"sub_question": "q", "sql": "```sql\nSELECT total FROM orders\n```"},
            {"done": True},
        ])
        run_investigation(
            "q", _schema(), plan_next=plan,
            execute=lambda sql: executed.append(sql) or [{"total": 1}],
            interpret=_interp(),
        )
        assert executed == ["SELECT total FROM orders"]   # fences stripped

    def test_execute_exception_is_caught_and_recorded(self):
        def boom(sql):
            raise RuntimeError("db down")
        plan = _planner([
            {"sub_question": "q", "sql": "SELECT total FROM orders"},
            {"done": True},
        ])
        res = run_investigation(
            "q", _schema(), plan_next=plan, execute=boom, interpret=_interp(),
        )
        assert "db down" in (res.steps[0].error or "")
        assert res.steps[0].row_count == 0

    def test_prior_steps_fed_back_to_planner(self):
        plan = _planner([
            {"sub_question": "q1", "sql": "SELECT total FROM orders"},
            {"sub_question": "q2", "sql": "SELECT total FROM orders"},
            {"done": True},
        ])
        run_investigation(
            "q", _schema(), plan_next=plan,
            execute=lambda sql: [{"total": 1}], interpret=_interp(),
        )
        # On the call that returned {done:True}, the planner had seen 2 steps.
        assert len(plan.seen["steps"]) == 2

    def test_empty_sql_without_done_breaks(self):
        plan = _planner([{"sub_question": "q", "sql": "   "}] * 5)
        res = run_investigation(
            "q", _schema(), plan_next=plan,
            execute=lambda sql: [{"x": 1}], interpret=_interp(),
            max_steps=5,
        )
        assert len(res.steps) == 0     # nothing executed, loop did not spin

    def test_consecutive_unsafe_aborts_early(self):
        executed = []
        # A planner stuck on unsafe SQL must abort before burning max_steps.
        plan = _planner([{"sub_question": "x", "sql": "DROP TABLE t"}] * 6)
        res = run_investigation(
            "q", _schema(), plan_next=plan,
            execute=lambda sql: executed.append(sql) or [],
            interpret=_interp(), max_steps=6,
        )
        assert executed == []
        assert len(res.steps) == 2          # stopped after 2 consecutive unsafe
        assert res.ok is False

    def test_all_failed_steps_skip_interpret(self):
        called = {"n": 0}

        def interp(q, s):
            called["n"] += 1
            return {"narrative": "x", "findings": [], "recommendations": [],
                    "chart": None}

        def boom(sql):
            raise RuntimeError("down")

        plan = _planner([
            {"sub_question": "x", "sql": "SELECT 1"},
            {"done": True},
        ])
        res = run_investigation(
            "q", _schema(), plan_next=plan, execute=boom, interpret=interp)
        assert called["n"] == 0     # no successful step -> interpret never called
        assert res.ok is False
        assert res.narrative        # still a graceful narrative


class TestInterpretation:
    def test_interpret_receives_steps_and_populates_result(self):
        interp = _interp({"narrative": "Revenue is up 10%.",
                          "findings": ["upward trend"],
                          "recommendations": ["scale ads"],
                          "chart": {"chart_type": "line", "x": "month",
                                    "y": "total", "title": "Revenue"}})
        plan = _planner([
            {"sub_question": "q", "sql": "SELECT total FROM orders"},
            {"done": True},
        ])
        res = run_investigation(
            "q", _schema(), plan_next=plan,
            execute=lambda sql: [{"total": 10}], interpret=interp,
        )
        assert len(interp.captured["steps"]) == 1
        assert res.narrative == "Revenue is up 10%."
        assert res.findings == ["upward trend"]
        assert res.recommendations == ["scale ads"]
        assert res.chart["chart_type"] == "line"

    def test_no_steps_skips_interpret_with_graceful_result(self):
        called = {"n": 0}

        def interp(q, s):
            called["n"] += 1
            return {"narrative": "x", "findings": [], "recommendations": [],
                    "chart": None}

        plan = _planner([{"done": True}])     # planner says done immediately
        res = run_investigation(
            "q", _schema(), plan_next=plan, execute=lambda sql: [],
            interpret=interp,
        )
        assert res.steps == []
        assert called["n"] == 0                # no data -> no LLM interpret call
        assert res.narrative                    # still a graceful narrative
        assert res.ok is False

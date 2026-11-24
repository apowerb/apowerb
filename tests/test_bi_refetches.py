"""Re-fetch counts from ADK session trace fixtures."""

import json
from importlib import import_module
from pathlib import Path

import pytest


@pytest.fixture
def count_refetches(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    return import_module("bench.bi_tokens.refetches").count_refetches


@pytest.fixture
def trace():
    def call(tool_id, args, name="tool_bi_query_dashboard"):
        return {"function_call": {"id": tool_id, "name": name, "args": args}}

    def result(tool_id, response):
        return {
            "function_response": {
                "id": tool_id,
                "name": "tool_bi_query_dashboard",
                "response": response,
            }
        }

    def span(contents, new_calls):
        return {
            "name": "call_llm",
            "attributes": {
                "gcp.vertex.agent.llm_request": json.dumps({"contents": contents}),
                "gcp.vertex.agent.llm_response": json.dumps(
                    {"content": {"role": "model", "parts": new_calls}}
                ),
            },
        }

    args = {"dashboard_id": 1, "components": ["roas"]}
    reordered_args = {"components": ["roas"], "dashboard_id": 1}
    other_args = {"dashboard_id": 2, "components": ["roas"]}
    prior_calls = {"role": "model", "parts": [call("a", args), call("b", other_args)]}
    full = {"value": 42}
    shortened = {"_compacted": "Result of an earlier turn, shortened to save tokens."}
    original_results = {"role": "user", "parts": [result("a", full), result("b", full)]}
    compacted_results = {
        "role": "user",
        "parts": [result("b", full), result("a", shortened)],
    }
    history = [prior_calls, compacted_results]
    refetch = call("c", reordered_args)
    return {
        "before_compaction": span([prior_calls, original_results], [refetch]),
        "changed_arguments": span(history, [call("c", other_args)]),
        "different_tool": span(history, [call("c", args, "tool_get_dashboard_data")]),
        "same_tool_and_arguments": span(history, [refetch]),
        "newer_full_result": span(
            history
            + [
                {"role": "model", "parts": [refetch]},
                {"role": "user", "parts": [result("c", full)]},
            ],
            [call("d", args)],
        ),
    }


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("before_compaction", 0),
        ("changed_arguments", 0),
        ("different_tool", 0),
        ("same_tool_and_arguments", 1),
        ("newer_full_result", 0),
    ],
)
def test_counts_refetches_on_session_trace(trace, count_refetches, case, expected):
    assert count_refetches([trace[case]]) == expected


def test_counts_each_new_call_once(trace, count_refetches):
    spans = [{"name": "execute_tool", "attributes": {}}, *trace.values()]
    assert count_refetches(spans) == 1


def test_requires_captured_request_contents(count_refetches):
    span = {"name": "call_llm", "attributes": {"gcp.vertex.agent.llm_request": "{}"}}
    with pytest.raises(ValueError, match="message content"):
        count_refetches([span])

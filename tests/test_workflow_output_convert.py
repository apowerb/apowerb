"""Nœuds Output et Convert (MVP demandé le 21/09).

Output désigne explicitement la sortie du workflow (au lieu de « la feuille
unique, sinon un dict de feuilles »). Convert change la forme d une valeur
(texte, JSON, nombre, booléen, liste) sans passer par un agent.
"""

import asyncio
import json

import pytest

from apowerb.core import workflow_graph as wg
from apowerb.core.workflow_engine import error_fields


def _run(nodes, edges, payload=None, agents=None):
    agents = agents or {}

    async def run_agent(agent_id, message):
        return agents.get(agent_id, f"out:{agent_id}")

    async def run_tool(tool, args):
        return {"tool": tool}

    async def go():
        out = []
        async for chunk in wg.run_graph(
            wg.WorkflowGraph.model_validate({"version": 1, "nodes": nodes, "edges": edges}),
            payload=payload,
            run_agent=run_agent,
            run_tool=run_tool,
            cancel_event=asyncio.Event(),
        ):
            out.append(json.loads(chunk[len("data: ") :]))
        return out

    return asyncio.run(go())


T = {"id": "t", "type": "trigger"}


def test_output_node_shapes_the_workflow_output_from_any_upstream_node():
    events = _run(
        [
            T,
            {"id": "a", "type": "agent", "config": {"agent_id": "agent1"}},
            {"id": "out", "type": "output", "config": {"value": {"answer": "{{a}}", "order": "{{t.id}}"}}},
        ],
        [{"source": "t", "target": "a"}, {"source": "a", "target": "out"}],
        payload={"id": "B7"},
    )
    assert events[-1] == {"event": "done", "output": {"answer": "out:agent1", "order": "B7"}}


def test_output_without_value_passes_its_input_through():
    events = _run(
        [T, {"id": "a", "type": "agent", "config": {"agent_id": "agent1"}}, {"id": "out", "type": "output"}],
        [{"source": "t", "target": "a"}, {"source": "a", "target": "out"}],
    )
    assert events[-1]["output"] == "out:agent1"


def test_output_with_an_empty_value_passes_its_input_through():
    events = _run(
        [T, {"id": "a", "type": "agent", "config": {"agent_id": "agent1"}}, {"id": "out", "type": "output", "config": {"value": ""}}],
        [{"source": "t", "target": "a"}, {"source": "a", "target": "out"}],
    )
    assert events[-1]["output"] == "out:agent1"


def test_output_node_cannot_have_successors():
    with pytest.raises(wg.GraphError, match="output"):
        wg.validate_graph(
            wg.WorkflowGraph.model_validate(
                {
                    "version": 1,
                    "nodes": [T, {"id": "out", "type": "output"}, {"id": "a", "type": "agent", "config": {"agent_id": "agent1"}}],
                    "edges": [{"source": "t", "target": "out"}, {"source": "out", "target": "a"}],
                }
            )
        )


@pytest.mark.parametrize(
    "value,to,expected",
    [
        ('```json\n{"a": 1}\n```', "json", {"a": 1}),
        ({"a": "é"}, "text", '{"a": "é"}'),
        ("hello", "text", "hello"),
        (" 12 ", "number", 12),
        ("3,5", "number", 3.5),
        (7, "number", 7),
        ("oui", "boolean", True),
        ("false", "boolean", False),
        (0, "boolean", False),
        ("a\n\n b \n", "list", ["a", "b"]),
        ('["x", 2]', "list", ["x", 2]),
        ({"k": 1}, "list", [{"k": 1}]),
        (None, "list", []),
    ],
)
def test_convert_value(value, to, expected):
    assert wg.convert_value(value, to) == expected


@pytest.mark.parametrize(
    "value,to",
    [("not json", "json"), ("abc", "number"), (True, "number"), ("maybe", "boolean")],
)
def test_convert_value_refuses_what_it_cannot_read(value, to):
    with pytest.raises(ValueError):
        wg.convert_value(value, to)


def test_convert_node_uses_its_input_template_and_reports_failures_with_a_code():
    ok = _run(
        [T, {"id": "c", "type": "convert", "config": {"to": "number", "input": "{{t.amount}}"}}],
        [{"source": "t", "target": "c"}],
        payload={"amount": "1200"},
    )
    assert ok[-1]["output"] == 1200

    bad = _run(
        [T, {"id": "c", "type": "convert", "config": {"to": "number"}}],
        [{"source": "t", "target": "c"}],
        payload={"amount": "x"},
    )
    err = next(e for e in bad if e["event"] == "node_error")
    assert err["code"] == "convert_failed"
    assert err["params"]["node"] == "c" and err["params"]["to"] == "number"


def test_convert_node_needs_a_known_target():
    with pytest.raises(wg.GraphError, match="conversion"):
        wg.validate_graph(
            wg.WorkflowGraph.model_validate(
                {"version": 1, "nodes": [T, {"id": "c", "type": "convert", "config": {"to": "xml"}}], "edges": [{"source": "t", "target": "c"}]}
            )
        )

"""Causes d erreur lisibles (demande B, 21/09).

Une erreur d exécution que l utilisateur peut corriger porte un ``code`` et
des ``params`` : l interface la rédige dans sa langue avec une piste
d action, au lieu d afficher « workflow_error » et un texte brut.
"""

import asyncio
import json

import pytest

from apowerb.core import workflow_graph as wg
from apowerb.core.workflow_engine import error_fields
from apowerb.core.workflow_runtime import call_tool


def _events(graph, tools=None, agents=None, payload=None):
    tools = tools or {}
    agents = agents or {}

    async def run_agent(agent_id, message):
        return agents.get(agent_id, "x")

    async def run_tool(tool, args):
        return await call_tool(tools[tool], args)

    async def run_rag(agent_id, query, top_k):
        return {"query": query, "passages": []}

    async def go():
        out = []
        async for chunk in wg.run_graph(
            wg.WorkflowGraph.model_validate(graph),
            payload=payload,
            run_agent=run_agent,
            run_tool=run_tool,
            run_rag=run_rag,
            cancel_event=asyncio.Event(),
        ):
            out.append(json.loads(chunk[len("data: ") :]))
        return out

    return asyncio.run(go())


def _node_error(events, node_id):
    return next(e for e in events if e["event"] == "node_error" and e["node_id"] == node_id)


def get_weather(city: str) -> str:
    return city


def test_tool_rejecting_its_arguments_is_named_with_the_problem():
    graph = {
        "version": 1,
        "nodes": [
            {"id": "t", "type": "trigger"},
            {"id": "w", "type": "tool", "config": {"tool": "weather", "args": {"town": "Metz"}}},
        ],
        "edges": [{"source": "t", "target": "w"}],
    }
    err = _node_error(_events(graph, tools={"weather": get_weather}), "w")
    assert err["code"] == "tool_arguments"
    assert err["params"]["tool"] == "get_weather"
    assert "city" in err["params"]["problem"]
    assert "ref" not in err


def test_unexpected_argument_is_named():
    with pytest.raises(wg.GraphError) as info:
        asyncio.run(call_tool(get_weather, {"city": "Metz", "town": "Metz"}))
    assert "town" in error_fields(info.value)["params"]["problem"]


def test_error_raised_inside_a_tool_is_not_mistaken_for_bad_arguments():
    def broken(city: str):
        raise TypeError("secret internal detail")

    fields = asyncio.run(_raises(broken, {"city": "x"}))
    assert fields["code"] == "internal"
    assert "secret internal detail" not in repr(fields)


async def _raises(func, args):
    try:
        await call_tool(func, args)
    except Exception as exc:  # noqa: BLE001
        return error_fields(exc)
    return None


def test_router_without_matching_rule_names_the_node():
    graph = {
        "version": 1,
        "nodes": [
            {"id": "t", "type": "trigger"},
            {
                "id": "r",
                "type": "router",
                "config": {"rules": [{"route": "a", "field": "{{t.x}}", "op": "eq", "value": 1}]},
            },
            {"id": "a", "type": "agent", "config": {"agent_id": "agent1"}},
        ],
        "edges": [{"source": "t", "target": "r"}, {"source": "r", "target": "a", "route": "a"}],
    }
    err = _node_error(_events(graph, payload={"x": 2}), "r")
    assert err["code"] == "no_route"
    assert err["params"] == {"node": "r"}


def test_classifier_answer_outside_its_routes():
    graph = {
        "version": 1,
        "nodes": [
            {"id": "t", "type": "trigger"},
            {
                "id": "c",
                "type": "classifier",
                "config": {"agent_id": "agent1", "routes": [{"route": "a"}, {"route": "b"}]},
            },
            {"id": "a", "type": "agent", "config": {"agent_id": "agent2"}},
            {"id": "b", "type": "agent", "config": {"agent_id": "agent2"}},
        ],
        "edges": [
            {"source": "t", "target": "c"},
            {"source": "c", "target": "a", "route": "a"},
            {"source": "c", "target": "b", "route": "b"},
        ],
    }
    err = _node_error(_events(graph, agents={"agent1": "no idea"}), "c")
    assert err["code"] == "classifier_no_route"
    assert err["params"] == {"node": "c", "routes": "a, b"}


def test_tool_needing_an_agent_context():
    def needs_ctx(tool_context):
        return 1

    with pytest.raises(wg.GraphError) as info:
        asyncio.run(call_tool(needs_ctx, {}))
    fields = error_fields(info.value)
    assert fields["code"] == "tool_needs_agent_context"
    assert fields["params"] == {"tool": "needs_ctx"}


def test_plain_user_errors_keep_the_generic_code():
    fields = error_fields(wg.GraphError("graphe vide"))
    assert fields == {"code": "workflow_error", "detail": "graphe vide"}

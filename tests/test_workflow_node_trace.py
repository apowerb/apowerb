"""Ce que chaque nœud a reçu et les outils qu'un agent a appelés.

Sans ces champs, le panneau Test ne montrait que les sorties : impossible de
voir qu'un agent recevait la seule réponse de l'agent précédent, sans la
demande d'origine (repro du 21/10/2026, agent-dev).
"""

import asyncio
import json

from apowerb.core import workflow_engine as we
from apowerb.core import workflow_graph as wg


def _graph(nodes, edges):
    return wg.WorkflowGraph.model_validate(
        {"version": 1, "nodes": nodes, "edges": edges}
    )


def _run(graph, run_agent, run_tool=None, payload=None):
    async def _tool(tool, args):
        return {"tool": tool}

    async def _go():
        out = []
        async for chunk in wg.run_graph(
            graph,
            payload=payload,
            run_agent=run_agent,
            run_tool=run_tool or _tool,
            cancel_event=asyncio.Event(),
        ):
            out.append(json.loads(chunk[len("data: ") :]))
        return out

    return asyncio.run(_go())


def _event(events, kind, node_id):
    return next(e for e in events if e["event"] == kind and e["node_id"] == node_id)


TWO_AGENTS = [
    {"id": "trigger1", "type": "trigger", "config": {"kind": "manual"}},
    {
        "id": "agent1",
        "type": "agent",
        "config": {"agent_id": "a1", "input": "{{trigger1}}"},
    },
    {"id": "agent2", "type": "agent", "config": {"agent_id": "a2"}},
]
TWO_EDGES = [
    {"source": "trigger1", "target": "agent1"},
    {"source": "agent1", "target": "agent2"},
]


def test_agent_input_is_emitted_before_the_agent_runs_and_kept_on_complete():
    received = {}
    order = []

    async def run_agent(agent_id, message):
        received[agent_id] = message
        order.append(("run", agent_id))
        return "Voici les chiffres clés" if agent_id == "a1" else "prévision"

    events = _run(
        _graph(TWO_AGENTS, TWO_EDGES), run_agent, payload={"message": "Analyse"}
    )

    # Entrée vide : agent2 reçoit la sortie d'agent1 sérialisée en JSON (une
    # chaîne arrive entre guillemets), et l'événement montre exactement cela.
    started = _event(events, "node_input", "agent2")
    assert started["input"] == received["a2"] == '"Voici les chiffres clés"'
    assert _event(events, "node_complete", "agent2")["input"] == received["a2"]
    # Gabarit : ce qui est montré est le message rendu, pas le gabarit.
    assert _event(events, "node_input", "agent1")["input"] == received["a1"]
    assert "{{" not in received["a1"]

    kinds = [(e["event"], e.get("node_id")) for e in events]
    assert kinds.index(("node_input", "agent2")) < kinds.index(
        ("node_complete", "agent2")
    )


def test_tool_node_shows_its_rendered_args():
    nodes = [
        {"id": "t", "type": "trigger", "config": {"kind": "manual"}},
        {
            "id": "look",
            "type": "tool",
            "config": {"tool": "erp.get", "args": {"id": "{{t.id}}"}},
        },
    ]

    async def run_agent(agent_id, message):
        return ""

    events = _run(
        _graph(nodes, [{"source": "t", "target": "look"}]),
        run_agent,
        payload={"id": "B7"},
    )
    assert _event(events, "node_input", "look")["input"] == {"id": "B7"}
    assert _event(events, "node_complete", "look")["input"] == {"id": "B7"}


def test_tool_calls_reported_by_run_agent_reach_node_complete():
    async def run_agent(agent_id, message):
        we.report_tool_calls(
            [{"name": "tool_list_datasets", "args": {}, "result": "[...]"}]
        )
        return "ok"

    nodes = TWO_AGENTS[:2]
    events = _run(_graph(nodes, TWO_EDGES[:1]), run_agent, payload={"message": "x"})
    done = _event(events, "node_complete", "agent1")
    assert done["tool_calls"] == [
        {"name": "tool_list_datasets", "args": {}, "result": "[...]"}
    ]


def test_tool_calls_do_not_leak_to_the_next_agent():
    async def run_agent(agent_id, message):
        if agent_id == "a1":
            we.report_tool_calls([{"name": "only_for_a1", "args": {}}])
        return "ok"

    events = _run(_graph(TWO_AGENTS, TWO_EDGES), run_agent, payload={"message": "x"})
    assert (
        _event(events, "node_complete", "agent1")["tool_calls"][0]["name"]
        == "only_for_a1"
    )
    assert _event(events, "node_complete", "agent2")["tool_calls"] == []


def test_report_tool_calls_outside_a_node_is_a_no_op():
    we.report_tool_calls([{"name": "x", "args": {}}])  # ne lève pas


def test_failed_agent_still_shows_what_it_received():
    async def run_agent(agent_id, message):
        raise RuntimeError("quota")

    nodes = TWO_AGENTS[:2]
    events = _run(
        _graph(nodes, TWO_EDGES[:1]), run_agent, payload={"message": "Analyse"}
    )
    err = _event(events, "node_error", "agent1")
    assert err["input"] == _event(events, "node_input", "agent1")["input"]
    assert "Analyse" in err["input"]


def test_huge_input_is_capped_in_events_but_not_for_the_agent():
    big = "x" * (wg.MAX_TRACE_INPUT_CHARS + 500)
    received = {}

    async def run_agent(agent_id, message):
        received[agent_id] = message
        return "ok"

    nodes = [
        {"id": "t", "type": "trigger", "config": {"kind": "manual"}},
        {
            "id": "a",
            "type": "agent",
            "config": {"agent_id": "a1", "input": "{{t.text}}"},
        },
    ]
    events = _run(
        _graph(nodes, [{"source": "t", "target": "a"}]),
        run_agent,
        payload={"text": big},
    )
    shown = _event(events, "node_input", "a")
    assert received["a1"] == big
    assert len(shown["input"]) < len(big)
    assert shown["input_truncated"] is True
    assert shown["input_length"] == len(big)


# --- Lecture des appels d'outils dans une réponse ADK ----------------------


def test_tool_calls_from_adk_response_pairs_calls_and_results():
    response = [
        {
            "content": {
                "parts": [
                    {
                        "functionCall": {
                            "name": "tool_list_datasets",
                            "args": {"q": "ventes"},
                        }
                    }
                ]
            }
        },
        {
            "content": {
                "parts": [
                    {
                        "functionResponse": {
                            "name": "tool_list_datasets",
                            "response": {"datasets": ["online_store_orders.csv"]},
                        }
                    }
                ]
            }
        },
        {
            "content": {
                "parts": [
                    {
                        "function_call": {
                            "name": "tool_create_chart",
                            "args": {"sql": "x"},
                        }
                    }
                ]
            }
        },
        {"content": {"parts": [{"text": "Voici les chiffres"}]}},
    ]
    calls = we.tool_calls_from_response(response)
    assert calls[0]["name"] == "tool_list_datasets"
    assert calls[0]["args"] == {"q": "ventes"}
    assert "online_store_orders.csv" in calls[0]["result"]
    # Un appel sans réponse (l'agent a fini avant, ou l'outil a planté) reste visible.
    assert calls[1] == {"name": "tool_create_chart", "args": {"sql": "x"}}


def test_tool_calls_from_response_caps_long_results():
    response = [
        {"content": {"parts": [{"functionCall": {"name": "t", "args": {}}}]}},
        {
            "content": {
                "parts": [
                    {"functionResponse": {"name": "t", "response": {"r": "y" * 50_000}}}
                ]
            }
        },
    ]
    call = we.tool_calls_from_response(response)[0]
    assert len(call["result"]) <= we.TRACE_TOOL_RESULT_CHARS
    assert call["result_truncated"] is True


def test_tool_calls_from_response_ignores_non_lists():
    assert we.tool_calls_from_response("texte") == []
    assert we.tool_calls_from_response({"text": "x"}) == []


def test_tool_call_args_are_capped_too():
    response = [
        {
            "content": {
                "parts": [
                    {"functionCall": {"name": "viz", "args": {"rows": ["r"] * 20_000}}}
                ]
            }
        },
    ]
    call = we.tool_calls_from_response(response)[0]
    assert len(call["args"]) <= we.TRACE_TOOL_RESULT_CHARS
    assert call["args_truncated"] is True


def test_oversized_output_error_still_shows_the_input():
    async def run_agent(agent_id, message):
        return "z" * (wg.MAX_NODE_OUTPUT_BYTES + 10)

    nodes = TWO_AGENTS[:2]
    events = _run(
        _graph(nodes, TWO_EDGES[:1]), run_agent, payload={"message": "Analyse"}
    )
    err = _event(events, "node_error", "agent1")
    assert err["code"] == "node_output_too_large"
    assert "Analyse" in err["input"]

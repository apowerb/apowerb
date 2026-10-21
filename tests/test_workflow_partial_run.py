"""« Tester jusqu'ici » : n'exécuter que l'amont d'un nœud (Studio proactif, étape 2).

Le Studio doit connaître la sortie d'un nœud pour composer l'entrée du
suivant, sans lancer tout le workflow : ``run_graph(until=...)`` n'exécute
que ce nœud et ses ancêtres, et ``done`` porte SA sortie.
"""

import asyncio
import json

from apowerb.core import workflow_graph as wg


def _graph(nodes, edges):
    return wg.WorkflowGraph.model_validate(
        {"version": 1, "nodes": nodes, "edges": edges}
    )


def _run(graph, *, until=None, payload=None, calls=None):
    calls = [] if calls is None else calls

    async def run_agent(agent_id, message):
        calls.append(("agent", agent_id))
        return f"réponse de {agent_id}"

    async def run_tool(tool, args):
        calls.append(("tool", tool))
        return {"rows": [1, 2], "tool": tool}

    async def _go():
        out = []
        async for chunk in wg.run_graph(
            graph,
            payload=payload,
            run_agent=run_agent,
            run_tool=run_tool,
            cancel_event=asyncio.Event(),
            until=until,
        ):
            out.append(json.loads(chunk[len("data: ") :]))
        return out

    return asyncio.run(_go()), calls


# trigger -> lookup (tool) -> writer (agent) -> notify ; trigger -> side (agent)
CHAIN = _graph(
    [
        {"id": "trigger1", "type": "trigger", "config": {"kind": "manual"}},
        {"id": "lookup", "type": "tool", "config": {"tool": "crm.lookup"}},
        {"id": "writer", "type": "agent", "config": {"agent_id": "w"}},
        {"id": "side", "type": "agent", "config": {"agent_id": "s"}},
        {
            "id": "notify",
            "type": "notification",
            "config": {"channel": "email", "to": ["ops@acme.fr"], "subject": "x", "body": "{{writer}}"},
        },
    ],
    [
        {"source": "trigger1", "target": "lookup"},
        {"source": "lookup", "target": "writer"},
        {"source": "writer", "target": "notify"},
        {"source": "trigger1", "target": "side"},
    ],
)


def test_until_runs_only_the_node_and_its_ancestors():
    events, calls = _run(CHAIN, until="lookup", payload={"id": 7})

    assert calls == [("tool", "crm.lookup")]
    started = {e["node_id"] for e in events if e["event"] == "node_start"}
    assert started == {"trigger1", "lookup"}
    assert events[-1] == {
        "event": "done",
        "output": {"rows": [1, 2], "tool": "crm.lookup"},
        "until": "lookup",
        "partial": True,
    }


def test_until_output_is_that_node_even_when_it_is_not_a_leaf():
    events, calls = _run(CHAIN, until="writer")

    assert ("agent", "s") not in calls  # la branche sœur n'est pas en amont
    assert ("agent", "w") in calls
    assert events[-1]["output"] == "réponse de w"
    assert events[-1]["until"] == "writer"


def test_until_on_a_merge_runs_every_one_of_its_inputs():
    graph = _graph(
        [
            {"id": "trigger1", "type": "trigger"},
            {"id": "a", "type": "agent", "config": {"agent_id": "a"}},
            {"id": "b", "type": "agent", "config": {"agent_id": "b"}},
            {"id": "m", "type": "merge"},
            {"id": "after", "type": "agent", "config": {"agent_id": "after"}},
        ],
        [
            {"source": "trigger1", "target": "a"},
            {"source": "trigger1", "target": "b"},
            {"source": "a", "target": "m"},
            {"source": "b", "target": "m"},
            {"source": "m", "target": "after"},
        ],
    )
    events, calls = _run(graph, until="m")

    assert sorted(calls) == [("agent", "a"), ("agent", "b")]
    assert events[-1]["output"] == {"a": "réponse de a", "b": "réponse de b"}


def test_unknown_until_node_is_refused_before_anything_runs():
    events, calls = _run(CHAIN, until="nope")

    assert calls == []
    assert [e["event"] for e in events] == ["error"]
    assert events[0]["code"] == "invalid_graph"
    assert events[0]["cause"] == "until_unknown_node"
    assert events[0]["params"] == {"node": "nope"}


def test_until_inside_a_loop_body_is_refused():
    events, calls = _run(CHAIN, until="loop1.inner")

    assert calls == []
    assert events[0]["cause"] == "until_unknown_node"


def test_until_not_reached_because_a_router_went_elsewhere_is_an_error():
    graph = _graph(
        [
            {"id": "trigger1", "type": "trigger"},
            {
                "id": "r",
                "type": "router",
                "config": {
                    "rules": [
                        {"route": "vip", "field": "{{trigger1.kind}}", "op": "eq", "value": "vip"}
                    ],
                    "default_route": "other",
                },
            },
            {"id": "vip", "type": "agent", "config": {"agent_id": "vip"}},
            {"id": "other", "type": "agent", "config": {"agent_id": "other"}},
        ],
        [
            {"source": "trigger1", "target": "r"},
            {"source": "r", "target": "vip", "route": "vip"},
            {"source": "r", "target": "other", "route": "other"},
        ],
    )
    events, calls = _run(graph, until="vip", payload={"kind": "standard"})

    assert calls == []
    assert events[-1]["event"] == "error"
    assert events[-1]["code"] == "until_not_reached"
    assert events[-1]["params"] == {"node": "vip"}


def test_without_until_the_whole_graph_still_runs():
    events, calls = _run(CHAIN)

    assert ("agent", "s") in calls and ("agent", "w") in calls
    assert "partial" not in events[-1]


def test_an_invalid_graph_is_refused_even_outside_the_until_scope():
    # Valider le graphe ENTIER : un « test jusqu'ici » ne doit pas faire
    # passer pour bon un graphe qui ne s'exécutera jamais en entier.
    graph = _graph(
        [
            {"id": "trigger1", "type": "trigger"},
            {"id": "a", "type": "agent", "config": {"agent_id": "a"}},
            {"id": "bad", "type": "set", "config": {"fields": []}},
        ],
        [
            {"source": "trigger1", "target": "a"},
            {"source": "a", "target": "bad"},
        ],
    )
    events, calls = _run(graph, until="a")

    assert calls == []
    assert events[0]["code"] == "invalid_graph"

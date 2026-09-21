"""Graphe de workflow (nœuds typés, arêtes routées) compilé en ``google.adk.workflow``.

Le canvas cesse d'être une liste d'agents : c'est un graphe persistable dont
les nœuds sont des agents existants, des outils du tools_store, des routeurs
(à règles ou par classifieur IA) et des fusions. L'exécution des agents et des
outils est injectée ; l'ordonnancement, le routage et la jointure passent par
le vrai moteur ADK.
"""

import asyncio
import json

import pytest

from apowerb.core import workflow_graph as wg


def _graph(nodes, edges):
    return wg.WorkflowGraph.model_validate(
        {"version": 1, "nodes": nodes, "edges": edges}
    )


class _Env:
    def __init__(self, agents=None, tools=None):
        self.agents = agents or {}
        self.tools = tools or {}
        self.calls = []

    async def run_agent(self, agent_id, message):
        self.calls.append(("agent", agent_id, message))
        out = self.agents.get(agent_id, f"out:{agent_id}")
        return out(message) if callable(out) else out

    async def run_tool(self, tool, args):
        self.calls.append(("tool", tool, args))
        out = self.tools.get(tool, {"tool": tool})
        return out(args) if callable(out) else out


def _run(graph, env, payload=None, cancel=None):
    async def _go():
        out = []
        async for chunk in wg.run_graph(
            graph,
            payload=payload,
            run_agent=env.run_agent,
            run_tool=env.run_tool,
            cancel_event=cancel or asyncio.Event(),
        ):
            out.append(json.loads(chunk[len("data: ") :]))
        return out

    return asyncio.run(_go())


def _done(events):
    assert events[-1]["event"] == "done", events
    return events[-1]["output"]


ORDER = [
    {"id": "start", "type": "trigger", "config": {"kind": "manual"}},
    {
        "id": "lookup",
        "type": "tool",
        "config": {
            "tool": "erp.tool_get_order",
            "args": {"order_id": "{{start.order_id}}"},
        },
    },
    {
        "id": "prio",
        "type": "router",
        "config": {
            "rules": [
                {
                    "route": "urgent",
                    "field": "{{lookup.amount}}",
                    "op": "gt",
                    "value": 1000,
                }
            ],
            "default_route": "normal",
        },
    },
    {"id": "urgent", "type": "agent", "config": {"agent_id": "agent32"}},
    {
        "id": "normal",
        "type": "agent",
        "config": {"agent_id": "agent12", "input": "Client {{lookup.client}}"},
    },
]
ORDER_EDGES = [
    {"source": "start", "target": "lookup"},
    {"source": "lookup", "target": "prio"},
    {"source": "prio", "target": "urgent", "route": "urgent"},
    {"source": "prio", "target": "normal", "route": "normal"},
]


def _order_env():
    return _Env(
        tools={
            "erp.tool_get_order": lambda a: {
                "order_id": a["order_id"],
                "client": "ACME",
                "amount": 12000 if a["order_id"].startswith("B") else 90,
            }
        }
    )


# --- Chemin nominal : trigger -> outil -> routeur -> agent -----------------


def test_tool_router_agent_takes_the_urgent_branch():
    env = _order_env()
    events = _run(_graph(ORDER, ORDER_EDGES), env, payload={"order_id": "B7"})
    assert env.calls[0] == ("tool", "erp.tool_get_order", {"order_id": "B7"})
    assert [c[1] for c in env.calls if c[0] == "agent"] == ["agent32"]
    assert json.loads(env.calls[1][2]) == {
        "order_id": "B7",
        "client": "ACME",
        "amount": 12000,
    }
    assert _done(events) == "out:agent32"
    route = next(e for e in events if e["event"] == "route")
    assert route == {"event": "route", "node_id": "prio", "route": "urgent"}


def test_default_route_and_template_input():
    env = _order_env()
    events = _run(_graph(ORDER, ORDER_EDGES), env, payload={"order_id": "A1"})
    assert [c[1:] for c in env.calls if c[0] == "agent"] == [("agent12", "Client ACME")]
    assert _done(events) == "out:agent12"


def test_node_events_carry_node_ids_in_order():
    events = _run(_graph(ORDER, ORDER_EDGES), _order_env(), payload={"order_id": "B7"})
    starts = [e["node_id"] for e in events if e["event"] == "node_start"]
    assert starts == ["start", "lookup", "prio", "urgent"]
    assert all("duration_ms" in e for e in events if e["event"] == "node_complete")


# --- Classifieur IA ---------------------------------------------------------


def test_classifier_routes_on_the_model_answer():
    env = _Env(agents={"agent5": " Facturation.\n", "agent6": "ok-fact"})
    g = _graph(
        [
            {"id": "t", "type": "trigger"},
            {
                "id": "cls",
                "type": "classifier",
                "config": {
                    "agent_id": "agent5",
                    "routes": [
                        {"route": "facturation", "description": "question de facture"},
                        {"route": "technique", "description": "panne"},
                    ],
                },
            },
            {"id": "f", "type": "agent", "config": {"agent_id": "agent6"}},
            {"id": "x", "type": "agent", "config": {"agent_id": "agent7"}},
        ],
        [
            {"source": "t", "target": "cls"},
            {"source": "cls", "target": "f", "route": "facturation"},
            {"source": "cls", "target": "x", "route": "technique"},
        ],
    )
    events = _run(g, env, payload={"text": "ma facture est fausse"})
    prompt = env.calls[0][2]
    assert (
        "facturation" in prompt
        and "technique" in prompt
        and "ma facture est fausse" in prompt
    )
    assert _done(events) == "ok-fact"


def test_classifier_without_a_valid_answer_fails_loudly():
    env = _Env(agents={"agent5": "je ne sais pas"})
    g = _graph(
        [
            {"id": "t", "type": "trigger"},
            {
                "id": "cls",
                "type": "classifier",
                "config": {
                    "agent_id": "agent5",
                    "routes": [{"route": "a"}, {"route": "b"}],
                },
            },
            {"id": "a", "type": "agent", "config": {"agent_id": "agent1"}},
            {"id": "b", "type": "agent", "config": {"agent_id": "agent2"}},
        ],
        [
            {"source": "t", "target": "cls"},
            {"source": "cls", "target": "a", "route": "a"},
            {"source": "cls", "target": "b", "route": "b"},
        ],
    )
    events = _run(g, env)
    assert events[-1]["event"] == "error"
    assert [c for c in env.calls if c[1] in ("agent1", "agent2")] == []


# --- Fan-out / fusion -------------------------------------------------------


def test_fan_out_then_merge_keys_outputs_by_node_id():
    env = _Env()
    g = _graph(
        [
            {"id": "t", "type": "trigger"},
            {"id": "a", "type": "agent", "config": {"agent_id": "agent1"}},
            {"id": "b", "type": "agent", "config": {"agent_id": "agent2"}},
            {"id": "m", "type": "merge"},
            {"id": "z", "type": "agent", "config": {"agent_id": "agent3"}},
        ],
        [
            {"source": "t", "target": "a"},
            {"source": "t", "target": "b"},
            {"source": "a", "target": "m"},
            {"source": "b", "target": "m"},
            {"source": "m", "target": "z"},
        ],
    )
    _done(_run(g, env))
    z_msg = next(c[2] for c in env.calls if c[1] == "agent3")
    assert json.loads(z_msg) == {"a": "out:agent1", "b": "out:agent2"}


# --- Validation (refus avant exécution) ------------------------------------


@pytest.mark.parametrize(
    "nodes, edges, needle",
    [
        (
            [{"id": "a", "type": "agent", "config": {"agent_id": "agent1"}}],
            [{"source": "a", "target": "ghost"}],
            "ghost",
        ),
        (
            [
                {"id": "a", "type": "agent", "config": {"agent_id": "agent1"}},
                {"id": "a", "type": "agent", "config": {"agent_id": "agent2"}},
            ],
            [],
            "dupliqu",
        ),
        (
            [
                {"id": "a", "type": "agent", "config": {"agent_id": "agent1"}},
                {"id": "b", "type": "agent", "config": {"agent_id": "agent2"}},
            ],
            [{"source": "a", "target": "b"}, {"source": "b", "target": "a"}],
            "cycle",
        ),
        (
            [
                {
                    "id": "r",
                    "type": "router",
                    "config": {
                        "rules": [
                            {"route": "x", "field": "{{r.v}}", "op": "eq", "value": 1}
                        ]
                    },
                },
                {"id": "b", "type": "agent", "config": {"agent_id": "agent2"}},
            ],
            [{"source": "r", "target": "b"}],
            "route",
        ),
        ([{"id": "a", "type": "agent", "config": {}}], [], "agent_id"),
        (
            [
                {
                    "id": "a",
                    "type": "agent",
                    "config": {"agent_id": "agent1", "input": "{{nope.x}}"},
                }
            ],
            [],
            "nope",
        ),
        ([{"id": "l", "type": "approval", "config": {}}], [], "approval"),
        ([], [], "vide"),
    ],
)
def test_invalid_graphs_are_refused_before_anything_runs(nodes, edges, needle):
    env = _Env()
    events = _run(_graph(nodes, edges), env)
    assert env.calls == []
    assert events[-1]["event"] == "error"
    assert needle in events[-1]["detail"].lower()


def test_router_rules_operators():
    ev = wg.evaluate_rule
    assert ev({"op": "eq", "value": "a"}, "a") and not ev(
        {"op": "ne", "value": "a"}, "a"
    )
    assert ev({"op": "gte", "value": 3}, 3) and ev({"op": "lt", "value": 3}, 2.5)
    assert ev({"op": "contains", "value": "fac"}, "ma facture")
    assert ev({"op": "in", "value": ["a", "b"]}, "b")
    assert ev({"op": "exists"}, 0) and not ev({"op": "exists"}, None)
    assert not ev({"op": "gt", "value": 3}, "pas un nombre")


def test_templates_resolve_nested_paths_and_keep_types():
    outputs = {"n": {"a": {"b": [1, {"c": "x"}]}}}
    assert wg.render("{{n.a.b.1.c}}", outputs) == "x"
    assert wg.render("{{n.a}}", outputs) == {"b": [1, {"c": "x"}]}
    assert wg.render("id={{n.a.b.0}}", outputs) == "id=1"
    assert wg.render({"k": ["{{n.a.b.0}}"]}, outputs) == {"k": [1]}
    assert wg.render("{{n.missing}}", outputs) is None


def test_failing_tool_reports_node_error_and_stops():
    env = _Env(tools={"t.boom": lambda a: (_ for _ in ()).throw(RuntimeError("boom"))})
    g = _graph(
        [
            {"id": "t", "type": "trigger"},
            {"id": "x", "type": "tool", "config": {"tool": "t.boom"}},
            {"id": "a", "type": "agent", "config": {"agent_id": "agent1"}},
        ],
        [{"source": "t", "target": "x"}, {"source": "x", "target": "a"}],
    )
    events = _run(g, env)
    assert any(e["event"] == "node_error" and e["node_id"] == "x" for e in events)
    assert events[-1]["event"] == "error"
    assert not any(c[0] == "agent" for c in env.calls)


# --- Boucle explicite (roadmap#55) -----------------------------------------

BODY = {
    "version": 1,
    "nodes": [
        {"id": "it", "type": "trigger"},
        {
            "id": "w",
            "type": "agent",
            "config": {
                "agent_id": "agent9",
                "input": "Traite {{it.item}} (#{{it.index}})",
            },
        },
    ],
    "edges": [{"source": "it", "target": "w"}],
}


def _loop_graph(loop_config):
    return _graph(
        [
            {"id": "t", "type": "trigger"},
            {"id": "l", "type": "loop", "config": loop_config},
            {"id": "z", "type": "agent", "config": {"agent_id": "agent3"}},
        ],
        [{"source": "t", "target": "l"}, {"source": "l", "target": "z"}],
    )


def test_foreach_runs_the_body_once_per_item_and_collects_outputs():
    env = _Env(agents={"agent9": lambda m: m.upper()})
    g = _loop_graph(
        {"mode": "foreach", "items": "{{t.rows}}", "max_iterations": 10, "body": BODY}
    )
    events = _run(g, env, payload={"rows": ["a", "b", "c"]})
    body_msgs = [c[2] for c in env.calls if c[1] == "agent9"]
    assert body_msgs == ["Traite a (#0)", "Traite b (#1)", "Traite c (#2)"]
    z_msg = next(c[2] for c in env.calls if c[1] == "agent3")
    assert json.loads(z_msg) == ["TRAITE A (#0)", "TRAITE B (#1)", "TRAITE C (#2)"]
    inner = [e for e in events if e["event"] == "node_start" and e["node_id"] == "l.w"]
    assert [e["iteration"] for e in inner] == [0, 1, 2]
    assert _done(events) == "out:agent3"


def test_foreach_stops_at_the_cap_and_says_so():
    env = _Env()
    g = _loop_graph(
        {"mode": "foreach", "items": "{{t.rows}}", "max_iterations": 2, "body": BODY}
    )
    events = _run(g, env, payload={"rows": [1, 2, 3, 4]})
    assert len([c for c in env.calls if c[1] == "agent9"]) == 2
    cap = [e for e in events if e["event"] == "loop_capped"]
    assert cap == [
        {"event": "loop_capped", "node_id": "l", "max_iterations": 2, "remaining": 2}
    ]


def test_until_repeats_until_the_condition_holds_and_chains_previous_output():
    body = {
        "version": 1,
        "nodes": [
            {"id": "it", "type": "trigger"},
            {
                "id": "w",
                "type": "agent",
                "config": {"agent_id": "agent9", "input": "{{it.previous}}"},
            },
        ],
        "edges": [{"source": "it", "target": "w"}],
    }
    env = _Env(
        agents={
            "agent9": lambda m: {
                "n": (json.loads(m)["n"] if m.startswith("{") else 0) + 1
            }
        }
    )
    g = _loop_graph(
        {
            "mode": "until",
            "max_iterations": 10,
            "body": body,
            "until": {"field": "{{iteration.output.n}}", "op": "gte", "value": 3},
        }
    )
    events = _run(g, env, payload={"n": 0})
    assert len([c for c in env.calls if c[1] == "agent9"]) == 3
    z_msg = next(c[2] for c in env.calls if c[1] == "agent3")
    assert json.loads(z_msg) == {"n": 3}
    _done(events)


@pytest.mark.parametrize(
    "cfg, needle",
    [
        ({"mode": "foreach", "items": "{{t.rows}}", "body": BODY}, "max_iterations"),
        (
            {
                "mode": "foreach",
                "items": "{{t.rows}}",
                "max_iterations": 500,
                "body": BODY,
            },
            "max_iterations",
        ),
        ({"mode": "foreach", "max_iterations": 3, "body": BODY}, "items"),
        ({"mode": "until", "max_iterations": 3, "body": BODY}, "until"),
        ({"mode": "foreach", "items": "{{t.rows}}", "max_iterations": 3}, "body"),
        (
            {
                "mode": "foreach",
                "items": "{{t.rows}}",
                "max_iterations": 3,
                "body": {
                    "version": 1,
                    "nodes": [{"id": "x", "type": "agent", "config": {}}],
                    "edges": [],
                },
            },
            "agent_id",
        ),
        ({"mode": "sometimes", "max_iterations": 3, "body": BODY}, "mode"),
    ],
)
def test_invalid_loops_are_refused_before_anything_runs(cfg, needle):
    env = _Env()
    events = _run(_loop_graph(cfg), env, payload={"rows": [1]})
    assert env.calls == []
    assert events[-1]["event"] == "error" and needle in events[-1]["detail"]


def test_foreach_on_a_non_list_is_a_clear_error():
    env = _Env()
    g = _loop_graph(
        {"mode": "foreach", "items": "{{t.rows}}", "max_iterations": 3, "body": BODY}
    )
    events = _run(g, env, payload={"rows": "pas une liste"})
    assert events[-1]["event"] == "error" and "liste" in events[-1]["detail"]

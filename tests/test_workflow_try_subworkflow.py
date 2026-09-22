"""Nœuds ``try`` et ``subworkflow`` (LOT 2, roadmap#55).

``try`` exécute un corps (même isolation qu'un ``loop``, voir ``_run_body``)
et route sur ``ok``/``error`` au lieu d'échouer le run ; ``subworkflow``
exécute un autre workflow enregistré via un callback résolu côté routeur,
avec les mêmes règles d'accès que pour le lancer directement.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from apowerb.core import workflow_graph as wg
from apowerb.core.workflow_runtime import call_tool


def _graph(nodes, edges):
    return wg.WorkflowGraph.model_validate(
        {"version": 1, "nodes": nodes, "edges": edges}
    )


class _Env:
    """Bouchon d'exécution : agents/outils/sous-workflows + trace des appels.

    Les outils passent par le vrai ``call_tool`` (liaison de signature),
    comme ``tests/test_workflow_error_codes.py`` : les fonctions déclarent
    leurs paramètres normalement (pas de dict brut).
    """

    def __init__(self, agents=None, tools=None, workflows=None):
        self.agents = agents or {}
        self.tools = tools or {}
        self.workflows = workflows or {}
        self.calls = []

    async def run_agent(self, agent_id, message):
        self.calls.append(("agent", agent_id, message))
        out = self.agents.get(agent_id, f"out:{agent_id}")
        return out(message) if callable(out) else out

    async def run_tool(self, tool, args):
        self.calls.append(("tool", tool, args))
        return await call_tool(self.tools[tool], args)

    async def run_subworkflow(self, workflow_id):
        self.calls.append(("subworkflow", workflow_id))
        graph = self.workflows.get(workflow_id)
        return wg.WorkflowGraph.model_validate(graph) if graph else None


def _run(graph, env, payload=None, cancel=None, workflow_id=None):
    async def _go():
        out = []
        async for chunk in wg.run_graph(
            graph,
            payload=payload,
            run_agent=env.run_agent,
            run_tool=env.run_tool,
            run_subworkflow=env.run_subworkflow,
            cancel_event=cancel or asyncio.Event(),
            workflow_id=workflow_id,
        ):
            out.append(json.loads(chunk[len("data: ") :]))
        return out

    return asyncio.run(_go())


def _done(events):
    assert events[-1]["event"] == "done", events
    return events[-1]["output"]


def _node_error(events, node_id):
    return next(
        e for e in events if e["event"] == "node_error" and e["node_id"] == node_id
    )


# --- try : exécution du corps ----------------------------------------------

BODY = {
    "version": 1,
    "nodes": [
        {"id": "it", "type": "trigger"},
        {"id": "w", "type": "agent", "config": {"agent_id": "agent9"}},
    ],
    "edges": [{"source": "it", "target": "w"}],
}


def _try_graph(try_config, extra_nodes=None, extra_edges=None):
    return _graph(
        [
            {"id": "t", "type": "trigger"},
            {"id": "s", "type": "try", "config": try_config},
            *(extra_nodes or []),
        ],
        [{"source": "t", "target": "s"}, *(extra_edges or [])],
    )


def _tool_body(tool="double"):
    return {
        "version": 1,
        "nodes": [
            {"id": "it", "type": "trigger"},
            {
                "id": "w",
                "type": "tool",
                "config": {"tool": tool, "args": {"n": "{{it.n}}"}},
            },
        ],
        "edges": [{"source": "it", "target": "w"}],
    }


def test_try_success_routes_to_the_ok_edge_with_the_body_output():
    env = _Env(
        tools={
            "double": lambda n: n * 2,
            "mark_ok": lambda: "reached_ok",
            "mark_err": lambda: "reached_err",
        }
    )
    g = _try_graph(
        {"body": _tool_body()},
        extra_nodes=[
            {
                "id": "ok_leaf",
                "type": "tool",
                "config": {"tool": "mark_ok", "args": {}},
            },
            {
                "id": "err_leaf",
                "type": "tool",
                "config": {"tool": "mark_err", "args": {}},
            },
        ],
        extra_edges=[
            {"source": "s", "target": "ok_leaf", "route": "ok"},
            {"source": "s", "target": "err_leaf", "route": "error"},
        ],
    )
    events = _run(g, env, payload={"n": 5})
    route_ev = next(e for e in events if e["event"] == "route" and e["node_id"] == "s")
    assert route_ev["route"] == "ok"
    assert env.calls == [("tool", "double", {"n": 5}), ("tool", "mark_ok", {})]
    assert _done(events) == "reached_ok"


def test_try_tool_failure_routes_to_error_with_the_readable_code():
    def get_weather(city: str) -> str:
        return city

    body = {
        "version": 1,
        "nodes": [
            {"id": "it", "type": "trigger"},
            {
                "id": "w",
                "type": "tool",
                "config": {"tool": "weather", "args": {"town": "Metz"}},
            },
        ],
        "edges": [{"source": "it", "target": "w"}],
    }
    env = _Env(tools={"weather": get_weather})
    g = _try_graph({"body": body})
    events = _run(g, env)
    out = _done(events)
    assert out["code"] == "tool_arguments"
    assert out["params"]["tool"] == "get_weather"
    assert out["node"] == "w"
    route_ev = next(e for e in events if e["event"] == "route" and e["node_id"] == "s")
    assert route_ev["route"] == "error"
    # le nœud interne du corps échoue bien (node_error "s.w", comme pour une
    # itération de loop) mais le nœud "s" lui-même ne relaie jamais l'échec :
    # ni node_error sur "s", ni erreur globale du run.
    assert [e["node_id"] for e in events if e["event"] == "node_error"] == ["s.w"]
    assert not [e for e in events if e["event"] == "error"]


def test_try_retries_the_exact_number_of_attempts():
    calls = []

    def always_fails(city: str) -> str:
        calls.append(city)
        raise RuntimeError("boom")

    body = {
        "version": 1,
        "nodes": [
            {"id": "it", "type": "trigger"},
            {
                "id": "w",
                "type": "tool",
                "config": {"tool": "x", "args": {"city": "Metz"}},
            },
        ],
        "edges": [{"source": "it", "target": "w"}],
    }
    env = _Env(tools={"x": always_fails})
    g = _try_graph({"body": body, "retries": 2})
    out = _done(_run(g, env))
    assert len(calls) == 3  # tentative initiale + 2 retries
    assert out["code"] == "internal"


def test_try_succeeds_on_a_later_attempt():
    calls = []

    def fails_twice(city: str) -> str:
        calls.append(city)
        if len(calls) < 3:
            raise RuntimeError("pas encore")
        return f"ok:{city}"

    body = {
        "version": 1,
        "nodes": [
            {"id": "it", "type": "trigger"},
            {
                "id": "w",
                "type": "tool",
                "config": {"tool": "x", "args": {"city": "Metz"}},
            },
        ],
        "edges": [{"source": "it", "target": "w"}],
    }
    env = _Env(tools={"x": fails_twice})
    g = _try_graph({"body": body, "retries": 2})
    events = _run(g, env)
    assert len(calls) == 3
    route_ev = next(e for e in events if e["event"] == "route" and e["node_id"] == "s")
    assert route_ev["route"] == "ok"
    assert _done(events) == "ok:Metz"


def test_try_internal_error_does_not_leak_the_raw_message():
    def broken(city: str):
        raise TypeError("secret internal detail")

    body = {
        "version": 1,
        "nodes": [
            {"id": "it", "type": "trigger"},
            {
                "id": "w",
                "type": "tool",
                "config": {"tool": "x", "args": {"city": "Metz"}},
            },
        ],
        "edges": [{"source": "it", "target": "w"}],
    }
    env = _Env(tools={"x": broken})
    g = _try_graph({"body": body})
    out = _done(_run(g, env))
    assert out["code"] == "internal"
    assert "secret internal detail" not in repr(out)


def test_try_failure_without_a_wired_error_edge_does_not_fail_the_run():
    body = {
        "version": 1,
        "nodes": [
            {"id": "it", "type": "trigger"},
            {"id": "w", "type": "tool", "config": {"tool": "x", "args": {}}},
        ],
        "edges": [{"source": "it", "target": "w"}],
    }

    def always_fails():
        raise RuntimeError("boom")

    env = _Env(tools={"x": always_fails})
    g = _try_graph({"body": body})  # aucune arête ok/error câblée depuis "s"
    events = _run(g, env)
    assert events[-1]["event"] == "done"
    out = events[-1]["output"]
    assert out["node"] == "w"


def test_try_cancellation_stops_retries_instead_of_replaying_the_body():
    cancel = asyncio.Event()
    calls = []

    def cancel_then_fail():
        calls.append(1)
        cancel.set()
        raise RuntimeError("boom")

    body = {
        "version": 1,
        "nodes": [
            {"id": "it", "type": "trigger"},
            {"id": "w", "type": "tool", "config": {"tool": "x", "args": {}}},
        ],
        "edges": [{"source": "it", "target": "w"}],
    }
    env = _Env(tools={"x": cancel_then_fail})
    g = _try_graph({"body": body, "retries": 3})
    events = _run(g, env, cancel=cancel)
    assert events[-1]["event"] == "cancelled"
    assert len(calls) == 1  # 1re tentative echoue et pose le cancel ; aucun retry


# --- try : validation --------------------------------------------------------


@pytest.mark.parametrize(
    "cfg, needle",
    [
        ({"retries": 4, "body": BODY}, "retries"),
        ({"retries": -1, "body": BODY}, "retries"),
        ({"retries": "2", "body": BODY}, "retries"),
        ({"retry_delay_ms": 5001, "body": BODY}, "retry_delay_ms"),
        ({"retry_delay_ms": -1, "body": BODY}, "retry_delay_ms"),
        ({}, "body"),
        (
            {
                "body": {
                    "version": 1,
                    "nodes": [
                        {"id": "w", "type": "agent", "config": {"agent_id": "a1"}}
                    ],
                    "edges": [],
                }
            },
            "corps",
        ),
    ],
)
def test_invalid_try_configs_are_refused_before_anything_runs(cfg, needle):
    env = _Env()
    events = _run(_try_graph(cfg), env)
    assert env.calls == []
    assert events[-1]["event"] == "error" and needle in events[-1]["detail"]


def test_try_body_cannot_contain_a_nested_loop():
    nested = {
        "version": 1,
        "nodes": [
            {"id": "it", "type": "trigger"},
            {
                "id": "l",
                "type": "loop",
                "config": {
                    "mode": "foreach",
                    "items": "{{it.rows}}",
                    "max_iterations": 3,
                    "body": BODY,
                },
            },
        ],
        "edges": [{"source": "it", "target": "l"}],
    }
    events = _run(_try_graph({"body": nested}), _Env())
    assert events[-1]["event"] == "error"
    assert "imbriqu" in events[-1]["detail"]


def test_try_body_cannot_contain_a_nested_try():
    nested = {
        "version": 1,
        "nodes": [
            {"id": "it", "type": "trigger"},
            {"id": "inner", "type": "try", "config": {"body": BODY}},
        ],
        "edges": [{"source": "it", "target": "inner"}],
    }
    events = _run(_try_graph({"body": nested}), _Env())
    assert events[-1]["event"] == "error"
    assert "imbriqu" in events[-1]["detail"]


def test_try_only_routes_ok_and_error_are_accepted():
    with pytest.raises(wg.GraphError, match="route"):
        wg.validate_graph(
            _try_graph(
                {"body": BODY},
                extra_nodes=[
                    {"id": "z", "type": "agent", "config": {"agent_id": "a1"}}
                ],
                extra_edges=[{"source": "s", "target": "z", "route": "maybe"}],
            )
        )


def test_try_refuses_more_than_one_edge_per_route():
    with pytest.raises(wg.GraphError, match="route"):
        wg.validate_graph(
            _try_graph(
                {"body": BODY},
                extra_nodes=[
                    {"id": "z1", "type": "agent", "config": {"agent_id": "a1"}},
                    {"id": "z2", "type": "agent", "config": {"agent_id": "a2"}},
                ],
                extra_edges=[
                    {"source": "s", "target": "z1", "route": "ok"},
                    {"source": "s", "target": "z2", "route": "ok"},
                ],
            )
        )


# --- subworkflow : exécution -------------------------------------------------


def _sub_graph(workflow_id, config_extra=None):
    return _graph(
        [
            {"id": "t", "type": "trigger"},
            {
                "id": "sw",
                "type": "subworkflow",
                "config": {"workflow_id": workflow_id, **(config_extra or {})},
            },
        ],
        [{"source": "t", "target": "sw"}],
    )


def test_subworkflow_runs_the_callee_and_returns_its_final_output():
    callee = _tool_body()
    env = _Env(tools={"double": lambda n: n * 2}, workflows={"wf1": callee})
    events = _run(_sub_graph("wf1"), env, payload={"n": 5})
    assert env.calls == [("subworkflow", "wf1"), ("tool", "double", {"n": 5})]
    inner_starts = [
        e["node_id"]
        for e in events
        if e["event"] == "node_start" and e["node_id"].startswith("sw.")
    ]
    assert inner_starts == ["sw.it", "sw.w"]
    assert _done(events) == 10


def test_subworkflow_input_template_overrides_the_default_payload():
    callee = {
        "version": 1,
        "nodes": [
            {"id": "it", "type": "trigger"},
            {"id": "w", "type": "agent", "config": {"agent_id": "agent7"}},
        ],
        "edges": [{"source": "it", "target": "w"}],
    }
    env = _Env(agents={"agent7": lambda m: m}, workflows={"wf1": callee})
    g = _graph(
        [
            {"id": "t", "type": "trigger"},
            {
                "id": "sw",
                "type": "subworkflow",
                "config": {"workflow_id": "wf1", "input": {"x": "{{t.value}}"}},
            },
        ],
        [{"source": "t", "target": "sw"}],
    )
    _run(g, env, payload={"value": 42})
    assert env.calls[1] == ("agent", "agent7", json.dumps({"x": 42}))


def test_subworkflow_not_found_is_a_readable_error():
    env = _Env(workflows={})
    events = _run(_sub_graph("ghost"), env)
    err = _node_error(events, "sw")
    assert err["code"] == "subworkflow_not_found"
    assert err["params"] == {"node": "sw", "workflow": "ghost"}


def test_subworkflow_access_denied_is_indistinguishable_from_not_found():
    # Le callback de production (workflow_runtime.resolve_workflow_for) filtre
    # par owner_id ; ici on simule directement son contrat : accès refusé ==
    # None, jamais une exception distincte qui confirmerait l'existence.
    env = _Env(workflows={})

    async def denies_everything(workflow_id):
        env.calls.append(("subworkflow", workflow_id))
        return None  # comme un workflow inexistant

    env.run_subworkflow = denies_everything
    events = _run(_sub_graph("someone-elses-workflow"), env)
    err = _node_error(events, "sw")
    assert err["code"] == "subworkflow_not_found"


def test_subworkflow_cycle_a_b_a_is_detected_at_runtime():
    wf_a = _sub_graph("wfB").model_dump(exclude_none=True)
    wf_b = _sub_graph("wfA").model_dump(exclude_none=True)
    # "wfA" volontairement absent de la table : la coupure doit avoir lieu
    # avant tout nouvel appel au callback (sur la pile), pas après un échec
    # de résolution.
    env = _Env(workflows={"wfB": wf_b})
    events = _run(wg.WorkflowGraph.model_validate(wf_a), env, workflow_id="wfA")
    err = _node_error(events, "sw.sw")
    assert err["code"] == "subworkflow_cycle"
    assert err["params"] == {"node": "sw", "workflow": "wfA"}
    assert env.calls == [("subworkflow", "wfB")]


def test_subworkflow_depth_over_three_is_refused():
    def sub_to(next_id):
        return _sub_graph(next_id).model_dump(exclude_none=True)

    env = _Env(
        workflows={
            "wf1": sub_to("wf2"),
            "wf2": sub_to("wf3"),
            # "wf3" et "wf4" volontairement absents : la profondeur coupe
            # avant que wf2 n'aille chercher wf3.
        }
    )
    events = _run(_sub_graph("wf1"), env)
    err = _node_error(events, "sw.sw.sw")
    assert err["code"] == "subworkflow_too_deep"
    assert err["params"] == {"node": "sw", "max": 3}
    assert [c for c in env.calls if c[0] == "subworkflow"] == [
        ("subworkflow", "wf1"),
        ("subworkflow", "wf2"),
    ]


def test_subworkflow_validation_requires_a_non_empty_workflow_id():
    with pytest.raises(wg.GraphError, match="workflow_id"):
        wg.validate_graph(_sub_graph(""))


def test_subworkflow_direct_self_reference_is_refused_at_validation_when_the_id_is_known():
    with pytest.raises(wg.GraphError, match="lui-même|cycle"):
        wg.validate_graph(_sub_graph("wfA"), workflow_id="wfA")
    # Sans identifiant connu (graphe pas encore enregistré), rien à comparer.
    wg.validate_graph(_sub_graph("wfA"))

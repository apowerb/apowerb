"""Gestion d'erreur par nœud : ``on_error`` (fail/continue/route) et ``timeout_s``.

``fail`` (défaut) arrête le run comme avant. ``continue`` laisse le flux
avancer mais l'erreur reste dans la trace (``handled_error`` du
``node_complete``) : elle ne doit jamais passer pour un résultat ordinaire.
``route`` ajoute une sortie ``error`` portant ``{code, detail, params,
node}`` comme celle du Try ; elle n'arrête jamais le run, et les arêtes de
succès ne partent PAS quand le nœud échoue (ADK déclenche toute arête sans
route : le moteur leur donne une route interne).
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from apowerb.core import workflow_graph as wg
from apowerb.core.workflow_engine import WorkflowCancelled

T = {"id": "t", "type": "trigger"}


def _graph(nodes, edges):
    return wg.WorkflowGraph.model_validate(
        {"version": 1, "nodes": nodes, "edges": edges}
    )


class _Env:
    def __init__(self, agents=None, tools=None, workflows=None):
        self.agents = agents or {}
        self.tools = tools or {}
        self.workflows = workflows or {}
        self.calls = []

    async def run_agent(self, agent_id, message):
        self.calls.append(("agent", agent_id))
        out = self.agents.get(agent_id, f"out:{agent_id}")
        if callable(out):
            out = out(message)
            if asyncio.iscoroutine(out):
                out = await out
        return out

    async def run_tool(self, tool, args):
        self.calls.append(("tool", tool))
        out = self.tools.get(tool, {"ok": True})
        return out(args) if callable(out) else out

    async def run_subworkflow(self, workflow_id):
        graph = self.workflows.get(workflow_id)
        return wg.WorkflowGraph.model_validate(graph) if graph else None


def _run(graph, env, cancel=None):
    async def _go():
        out = []
        async for chunk in wg.run_graph(
            graph,
            payload={},
            run_agent=env.run_agent,
            run_tool=env.run_tool,
            run_subworkflow=env.run_subworkflow,
            cancel_event=cancel or asyncio.Event(),
        ):
            out.append(json.loads(chunk[len("data: ") :]))
        return out

    return asyncio.run(_go())


def _boom(_message):
    raise wg.GraphError("le modèle a refusé", code="agent_boom", params={"why": "x"})


def _agent(on_error=None, **cfg):
    config = {"agent_id": "a1", **cfg}
    if on_error is not None:
        config["on_error"] = on_error
    return {"id": "a", "type": "agent", "config": config}


OK = {"id": "ok", "type": "output", "config": {"value": "ok:{{a}}"}}
KO = {"id": "ko", "type": "output", "config": {"value": "{{a}}"}}
ROUTED_EDGES = [
    {"source": "t", "target": "a"},
    {"source": "a", "target": "ok"},
    {"source": "a", "target": "ko", "route": "error"},
]


def _started(events, node_id):
    return any(
        e["event"] == "node_start" and e.get("node_id") == node_id for e in events
    )


def _complete(events, node_id):
    return next(
        e for e in events if e["event"] == "node_complete" and e["node_id"] == node_id
    )


def _code(nodes, edges):
    with pytest.raises(wg.GraphError) as exc:
        wg.validate_graph(_graph(nodes, edges))
    return exc.value.code


# --- Validation ---------------------------------------------------------------


def test_on_error_route_with_its_error_output_wired_is_valid():
    wg.validate_graph(_graph([T, _agent("route"), OK, KO], ROUTED_EDGES))


def test_unknown_on_error_value_is_ignored_with_a_warning(caplog):
    # Un graphe enregistré doit continuer de s'exécuter ; le Studio le signale.
    graph = _graph([T, _agent("ignore"), OK], ROUTED_EDGES[:2])
    with caplog.at_level("WARNING"):
        wg.validate_graph(graph)
    assert "on_error" in caplog.text
    events = _run(graph, _Env(agents={"a1": _boom}))
    assert events[-1]["event"] == "error"


def test_a_null_time_limit_means_none_like_a_null_on_error():
    sub = {
        "id": "a",
        "type": "subworkflow",
        "config": {"workflow_id": "w2", "timeout_s": None},
    }
    wg.validate_graph(_graph([T, sub], [{"source": "t", "target": "a"}]))


def test_a_null_on_error_means_fail_like_in_the_studio():
    wg.validate_graph(_graph([T, _agent(None), OK], ROUTED_EDGES[:2]))
    node = _agent()
    node["config"]["on_error"] = None
    wg.validate_graph(_graph([T, node, OK], ROUTED_EDGES[:2]))
    events = _run(_graph([T, node, OK], ROUTED_EDGES[:2]), _Env(agents={"a1": _boom}))
    assert events[-1]["event"] == "error"


def test_fail_is_accepted_everywhere_as_the_default():
    nodes = [
        T,
        {
            "id": "s",
            "type": "set",
            "config": {"fields": [{"key": "k", "value": "v"}], "on_error": "fail"},
        },
    ]
    wg.validate_graph(_graph(nodes, [{"source": "t", "target": "s"}]))


def test_an_unwired_error_output_is_refused():
    assert _code([T, _agent("route"), OK], ROUTED_EDGES[:2]) == "on_error_route_missing"


def test_an_error_edge_needs_on_error_route():
    assert _code([T, _agent(), OK, KO], ROUTED_EDGES) == "on_error_route_unexpected"


def test_a_route_node_only_knows_its_error_route():
    edges = ROUTED_EDGES[:2] + [{"source": "a", "target": "ko", "route": "oops"}]
    with pytest.raises(wg.GraphError):
        wg.validate_graph(_graph([T, _agent("route"), OK, KO], edges))


def test_classifier_cannot_continue_but_can_route():
    cls = {
        "id": "c",
        "type": "classifier",
        "config": {
            "agent_id": "a1",
            "routes": [{"route": "x"}, {"route": "y"}],
            "on_error": "continue",
        },
    }
    nodes = [
        T,
        cls,
        {"id": "x", "type": "output"},
        {"id": "y", "type": "output"},
        {"id": "e", "type": "output"},
    ]
    edges = [
        {"source": "t", "target": "c"},
        {"source": "c", "target": "x", "route": "x"},
        {"source": "c", "target": "y", "route": "y"},
    ]
    wg.validate_graph(_graph(nodes, edges))  # continue ignoré : vaut fail
    cls["config"]["on_error"] = "route"
    assert _code(nodes, edges) == "on_error_route_missing"
    wg.validate_graph(
        _graph(nodes, edges + [{"source": "c", "target": "e", "route": "error"}])
    )


def test_classifier_route_named_error_conflicts_with_the_error_output():
    cls = {
        "id": "c",
        "type": "classifier",
        "config": {
            "agent_id": "a1",
            "routes": [{"route": "error"}, {"route": "y"}],
            "on_error": "route",
        },
    }
    nodes = [T, cls, {"id": "y", "type": "output"}, {"id": "e", "type": "output"}]
    edges = [
        {"source": "t", "target": "c"},
        {"source": "c", "target": "y", "route": "y"},
        {"source": "c", "target": "e", "route": "error"},
    ]
    assert _code(nodes, edges) == "on_error_route_conflict"


@pytest.mark.parametrize("value", [0, 3601, True, "10", 1.5])
def test_timeout_out_of_bounds_is_refused(value):
    sub = {
        "id": "a",
        "type": "subworkflow",
        "config": {"workflow_id": "w2", "timeout_s": value},
    }
    assert _code([T, sub], [{"source": "t", "target": "a"}]) == "node_timeout_invalid"


def test_error_fields_are_readable_downstream_of_a_handled_node():
    out = {
        "id": "ko",
        "type": "output",
        "config": {"value": "{{h.code}} {{h.params.status}}"},
    }
    http = {
        "id": "h",
        "type": "http",
        "config": {"method": "GET", "url": "https://x.example", "on_error": "route"},
    }
    edges = [
        {"source": "t", "target": "h"},
        {"source": "h", "target": "ko", "route": "error"},
    ]
    wg.validate_graph(_graph([T, http, out], edges))
    http["config"]["on_error"] = "fail"
    with pytest.raises(wg.GraphError, match="pas de champ"):
        wg.validate_graph(
            _graph([T, http, out], [edges[0], {"source": "h", "target": "ko"}])
        )


# --- fail : inchangé ------------------------------------------------------------


def test_fail_still_stops_the_run():
    env = _Env(agents={"a1": _boom})
    events = _run(_graph([T, _agent(), OK], ROUTED_EDGES[:2]), env)
    assert events[-1]["event"] == "error"
    assert not _started(events, "ok")


# --- continue -------------------------------------------------------------------


def test_continue_goes_on_and_keeps_the_error_in_the_trace():
    env = _Env(agents={"a1": _boom})
    events = _run(
        _graph(
            [T, _agent("continue"), KO],
            [ROUTED_EDGES[0], {"source": "a", "target": "ko"}],
        ),
        env,
    )
    assert events[-1]["event"] == "done"
    done = _complete(events, "a")
    # Le contre-exemple : sans handled_error, le panneau Test montrerait un
    # nœud réussi alors que l'agent a échoué.
    assert done["handled_error"]["code"] == "agent_boom"
    assert done["handled_error"]["on_error"] == "continue"
    assert done["output"] == {
        "code": "agent_boom",
        "detail": "le modèle a refusé",
        "params": {"why": "x"},
        "node": "a",
    }
    assert not any(e["event"] == "node_error" for e in events)


def test_continue_on_a_tool_that_raises_stops_the_run_as_before():
    def raising(_args):
        raise wg.GraphError("panne", code="tool_down")

    env = _Env(tools={"x.y": raising})
    tool = {
        "id": "a",
        "type": "tool",
        "config": {"tool": "x.y", "args": {}, "on_error": "continue"},
    }
    events = _run(
        _graph([T, tool, KO], [ROUTED_EDGES[0], {"source": "a", "target": "ko"}]), env
    )
    assert events[-1]["event"] == "error"
    assert next(e for e in events if e["event"] == "node_error")["code"] == "tool_down"
    assert not _started(events, "ko")


def test_route_on_a_tool_that_raises_takes_the_error_output():
    def raising(_args):
        raise wg.GraphError("panne", code="tool_down")

    tool = {
        "id": "a",
        "type": "tool",
        "config": {"tool": "x.y", "args": {}, "on_error": "route"},
    }
    events = _run(_graph([T, tool, OK, KO], ROUTED_EDGES), _Env(tools={"x.y": raising}))
    assert events[-1]["output"]["code"] == "tool_down"
    assert not _started(events, "ok")


def test_continue_on_a_reported_tool_error_keeps_the_tool_result():
    result = {"status": "error", "message": "boom"}
    env = _Env(tools={"x.y": result})
    tool = {
        "id": "a",
        "type": "tool",
        "config": {"tool": "x.y", "args": {}, "on_error": "continue"},
    }
    events = _run(
        _graph([T, tool, KO], [ROUTED_EDGES[0], {"source": "a", "target": "ko"}]), env
    )
    assert events[-1] == {"event": "done", "output": result}
    done = _complete(events, "a")
    assert done["handled_error"]["code"] == "tool_reported_error"
    assert done["reported_error"] == "boom"


def test_continue_never_swallows_a_cancellation():
    def cancelled(_args):
        raise WorkflowCancelled()

    env = _Env(tools={"x.y": cancelled})
    tool = {
        "id": "a",
        "type": "tool",
        "config": {"tool": "x.y", "args": {}, "on_error": "continue"},
    }
    events = _run(
        _graph([T, tool, KO], [ROUTED_EDGES[0], {"source": "a", "target": "ko"}]), env
    )
    assert events[-1]["event"] != "done"
    assert not _started(events, "ko")


# --- route ------------------------------------------------------------------------


def test_route_takes_the_error_output_and_never_the_success_one():
    env = _Env(agents={"a1": _boom})
    events = _run(_graph([T, _agent("route"), OK, KO], ROUTED_EDGES), env)
    assert events[-1]["event"] == "done"
    assert events[-1]["output"] == {
        "code": "agent_boom",
        "detail": "le modèle a refusé",
        "params": {"why": "x"},
        "node": "a",
    }
    # Contre-exemple : une arête sans route part toujours dans ADK.
    assert not _started(events, "ok")
    assert {"event": "route", "node_id": "a", "route": "error"} in events
    assert _complete(events, "a")["handled_error"]["on_error"] == "route"


def test_route_on_success_takes_the_success_output_only():
    env = _Env()
    events = _run(_graph([T, _agent("route"), OK, KO], ROUTED_EDGES), env)
    assert events[-1] == {"event": "done", "output": "ok:out:a1"}
    assert not _started(events, "ko")
    assert not any(e["event"] == "route" for e in events)


def test_route_never_stops_the_run_even_with_a_dead_end_error_branch():
    env = _Env(agents={"a1": _boom})
    end = {
        "id": "ko",
        "type": "set",
        "config": {"fields": [{"key": "seen", "value": "{{a.code}}"}]},
    }
    events = _run(_graph([T, _agent("route"), OK, end], ROUTED_EDGES), env)
    assert events[-1] == {"event": "done", "output": {"seen": "agent_boom"}}


def test_route_on_a_reported_tool_error():
    env = _Env(tools={"x.y": {"success": False, "error": "refusé"}})
    tool = {
        "id": "a",
        "type": "tool",
        "config": {"tool": "x.y", "args": {}, "on_error": "route"},
    }
    events = _run(_graph([T, tool, OK, KO], ROUTED_EDGES), env)
    assert events[-1]["output"]["code"] == "tool_reported_error"
    assert not _started(events, "ok")


def test_classifier_route_on_error():
    env = _Env(agents={"a1": "aucune idée"})
    cls = {
        "id": "a",
        "type": "classifier",
        "config": {
            "agent_id": "a1",
            "routes": [{"route": "x"}, {"route": "y"}],
            "on_error": "route",
        },
    }
    nodes = [T, cls, {"id": "x", "type": "output"}, {"id": "y", "type": "output"}, KO]
    edges = [
        {"source": "t", "target": "a"},
        {"source": "a", "target": "x", "route": "x"},
        {"source": "a", "target": "y", "route": "y"},
        {"source": "a", "target": "ko", "route": "error"},
    ]
    events = _run(_graph(nodes, edges), env)
    assert events[-1]["output"]["code"] == "classifier_no_route"
    assert not _started(events, "x") and not _started(events, "y")


def test_subworkflow_error_names_the_inner_node():
    inner = {
        "version": 1,
        "nodes": [
            {"id": "it", "type": "trigger"},
            {"id": "deep", "type": "agent", "config": {"agent_id": "a1"}},
        ],
        "edges": [{"source": "it", "target": "deep"}],
    }
    env = _Env(agents={"a1": _boom}, workflows={"w2": inner})
    sub = {
        "id": "a",
        "type": "subworkflow",
        "config": {"workflow_id": "w2", "on_error": "route"},
    }
    events = _run(_graph([T, sub, OK, KO], ROUTED_EDGES), env)
    assert events[-1]["output"]["node"] == "deep"
    assert events[-1]["output"]["code"] == "agent_boom"


def test_a_handled_node_inside_a_try_body_does_not_trip_the_try():
    body = {
        "nodes": [{"id": "bt", "type": "trigger"}, _agent("continue")],
        "edges": [{"source": "bt", "target": "a"}],
    }
    env = _Env(agents={"a1": _boom})
    nodes = [
        T,
        {"id": "try1", "type": "try", "config": {"body": body}},
        {"id": "ok", "type": "output", "config": {"value": "ok"}},
        {"id": "ko", "type": "output", "config": {"value": "ko"}},
    ]
    edges = [
        {"source": "t", "target": "try1"},
        {"source": "try1", "target": "ok", "route": "ok"},
        {"source": "try1", "target": "ko", "route": "error"},
    ]
    events = _run(_graph(nodes, edges), env)
    assert events[-1] == {"event": "done", "output": "ok"}


def _snapshots(graph, env):
    saved = []

    async def _go():
        async for _chunk in wg.run_graph(
            graph,
            payload={},
            run_agent=env.run_agent,
            run_tool=env.run_tool,
            run_subworkflow=env.run_subworkflow,
            cancel_event=asyncio.Event(),
            on_fail=saved.append,
        ):
            pass

    asyncio.run(_go())
    return saved


def test_a_handled_failure_is_never_the_node_to_retry_from():
    # « Reprendre ici » repart du nœud en échec : jamais d'un nœud dont
    # on_error a absorbé l'échec.
    env = _Env(agents={"a1": _boom})
    assert _snapshots(_graph([T, _agent("route"), OK, KO], ROUTED_EDGES), env) == []
    after = {"id": "b", "type": "agent", "config": {"agent_id": "b1"}}
    edges = [*ROUTED_EDGES[:2], {"source": "a", "target": "b", "route": "error"}]
    env = _Env(agents={"a1": _boom, "b1": _boom})
    saved = _snapshots(_graph([T, _agent("route"), OK, after], edges), env)
    assert [s["failed_node"] for s in saved] == ["b"]


def test_a_retry_still_shows_the_handled_error_of_a_replayed_node():
    # « Reprendre ici » rejoue l'amont : le nœud absorbé reste marqué.
    after = {"id": "b", "type": "agent", "config": {"agent_id": "b1"}}
    graph = _graph(
        [T, _agent("continue"), after],
        [*ROUTED_EDGES[:1], {"source": "a", "target": "b"}],
    )
    saved = _snapshots(graph, _Env(agents={"a1": _boom, "b1": _boom}))
    assert [s["failed_node"] for s in saved] == ["b"]
    resume = {k: saved[0].get(k) for k in wg.RESUME_KEYS}

    async def _go():
        env = _Env(agents={"b1": _boom})
        return [
            json.loads(c[len("data: ") :])
            async for c in wg.run_graph(
                graph,
                payload={},
                run_agent=env.run_agent,
                run_tool=env.run_tool,
                run_subworkflow=env.run_subworkflow,
                cancel_event=asyncio.Event(),
                resume=resume,
            )
        ]

    replayed = _complete(asyncio.run(_go()), "a")
    assert replayed["replayed"] is True
    assert replayed["handled_error"]["on_error"] == "continue"


# --- timeout_s ----------------------------------------------------------------------


class _Slow:
    """Un agent qui dort ; note s'il a été vraiment annulé."""

    def __init__(self, seconds=5):
        self.seconds = seconds
        self.cancelled = False

    async def __call__(self, _message):
        try:
            await asyncio.sleep(self.seconds)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return "trop tard"


def _slow_inner(agent="a1"):
    return {
        "version": 1,
        "nodes": [
            {"id": "it", "type": "trigger"},
            {"id": "slow", "type": "agent", "config": {"agent_id": agent}},
        ],
        "edges": [{"source": "it", "target": "slow"}],
    }


def _sub(on_error=None, **cfg):
    config = {"workflow_id": "w2", **cfg}
    if on_error is not None:
        config["on_error"] = on_error
    return {"id": "a", "type": "subworkflow", "config": config}


def test_timeout_fails_the_node_and_really_cancels_it():
    slow = _Slow()
    env = _Env(agents={"a1": slow}, workflows={"w2": _slow_inner()})
    t0 = time.monotonic()
    events = _run(_graph([T, _sub(timeout_s=1), OK], ROUTED_EDGES[:2]), env)
    assert time.monotonic() - t0 < 3
    err = next(e for e in events if e["event"] == "node_error" and e["node_id"] == "a")
    assert err["code"] == "node_timeout"
    assert err["params"] == {"node": "a", "seconds": "1"}
    assert slow.cancelled
    assert events[-1]["event"] == "error"


def test_timeout_follows_on_error_route():
    env = _Env(agents={"a1": _Slow()}, workflows={"w2": _slow_inner()})
    events = _run(_graph([T, _sub("route", timeout_s=1), OK, KO], ROUTED_EDGES), env)
    assert events[-1]["output"]["code"] == "node_timeout"
    assert not _started(events, "ok")


def test_a_fast_node_is_untouched_by_its_timeout():
    env = _Env(workflows={"w2": _slow_inner()})
    events = _run(_graph([T, _sub(timeout_s=1), OK], ROUTED_EDGES[:2]), env)
    assert events[-1]["event"] == "done"


def test_a_timeout_raised_inside_the_node_is_not_its_time_limit():
    # Contre-exemple de la garde ``deadline.expired()`` : un TimeoutError
    # levé par le nœud lui-même (base, réseau) garde son propre sens.
    env = _Env()

    async def loading(_workflow_id):
        raise TimeoutError("base de données lente")

    env.run_subworkflow = loading
    events = _run(_graph([T, _sub(timeout_s=30), OK], ROUTED_EDGES[:2]), env)
    assert events[-1]["event"] == "error"
    assert all(e.get("code") != "node_timeout" for e in events)


def test_subworkflow_timeout_stops_the_inner_run():
    slow = _Slow()
    inner = {
        "version": 1,
        "nodes": [
            {"id": "it", "type": "trigger"},
            {"id": "slow", "type": "agent", "config": {"agent_id": "a1"}},
            {"id": "after", "type": "agent", "config": {"agent_id": "a2"}},
        ],
        "edges": [
            {"source": "it", "target": "slow"},
            {"source": "slow", "target": "after"},
        ],
    }
    env = _Env(agents={"a1": slow}, workflows={"w2": inner})
    sub = {
        "id": "a",
        "type": "subworkflow",
        "config": {"workflow_id": "w2", "timeout_s": 1},
    }

    async def _go():
        out = []
        async for chunk in wg.run_graph(
            _graph([T, sub], [{"source": "t", "target": "a"}]),
            payload={},
            run_agent=env.run_agent,
            run_tool=env.run_tool,
            run_subworkflow=env.run_subworkflow,
            cancel_event=asyncio.Event(),
        ):
            out.append(json.loads(chunk[len("data: ") :]))
        # Laisse à un nœud interne survivant le temps de démarrer.
        await asyncio.sleep(slow.seconds + 0.5)
        return out

    events = asyncio.run(_go())
    assert (
        next(e for e in events if e["event"] == "node_error")["code"] == "node_timeout"
    )
    assert slow.cancelled
    assert ("agent", "a2") not in env.calls


# --- reprise après une décision ---------------------------------------------------


def test_resume_replays_a_route_node_on_its_success_output():
    nodes = [
        T,
        _agent("route"),
        {"id": "d", "type": "approval", "config": {"question": "Q", "mode": "text"}},
        {"id": "ok", "type": "output", "config": {"value": "ok:{{d.text}}"}},
        KO,
    ]
    edges = [
        {"source": "t", "target": "a"},
        {"source": "a", "target": "d"},
        {"source": "a", "target": "ko", "route": "error"},
        {"source": "d", "target": "ok"},
    ]
    env, saved = _Env(), []

    async def _go(resume=None):
        async def _on_await(snapshot):
            saved.append(snapshot)

        out = []
        async for chunk in wg.run_graph(
            _graph(nodes, edges),
            payload={},
            run_agent=env.run_agent,
            run_tool=env.run_tool,
            cancel_event=asyncio.Event(),
            on_await=_on_await,
            resume=resume,
            run_id="r1",
        ):
            out.append(json.loads(chunk[len("data: ") :]))
        return out

    asyncio.run(_go())
    answer = {
        "node_id": "d",
        "choice": None,
        "text": "vu",
        "answered_by": "u",
        "answered_at": "x",
    }
    events = asyncio.run(_go({**saved[0], "answer": answer}))
    assert events[-1] == {"event": "done", "output": "ok:vu"}
    assert not _started(events, "ko")

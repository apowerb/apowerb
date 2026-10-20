"""Un outil qui SIGNALE une erreur dans son résultat fait échouer son nœud.

Les outils du portfolio ne lèvent presque jamais : ils renvoient
``{"status": "error", ...}`` ou ``{"success": False, ...}``. Compté comme un
succès, ce résultat faisait avancer le graphe sur une donnée vide (OCR raté →
classification vide → route par défaut → run « done » avec un résultat faux).
``on_error: "continue"`` garde l'ancien comportement, nœud par nœud.
"""

import asyncio
import json

import pytest

from apowerb.core import workflow_graph as wg

T = {"id": "t", "type": "trigger"}


def _run(nodes, edges, tool_result):
    async def run_tool(tool, args):
        return tool_result

    async def run_agent(agent_id, message):
        pytest.fail("pas d'agent")

    async def go():
        out = []
        async for chunk in wg.run_graph(
            wg.WorkflowGraph.model_validate(
                {"version": 1, "nodes": nodes, "edges": edges}
            ),
            payload={},
            run_agent=run_agent,
            run_tool=run_tool,
            cancel_event=asyncio.Event(),
        ):
            out.append(json.loads(chunk[len("data: ") :]))
        return out

    return asyncio.run(go())


def _tool(on_error=None):
    cfg = {"tool": "x.y", "args": {}}
    if on_error is not None:
        cfg["on_error"] = on_error
    return {"id": "tool", "type": "tool", "config": cfg}


OUT = {"id": "out", "type": "output", "config": {"value": "{{tool}}"}}
EDGES = [{"source": "t", "target": "tool"}, {"source": "tool", "target": "out"}]


@pytest.mark.parametrize(
    "result, message",
    [
        ({"status": "error", "error_message": "File not found: a.pdf"}, "File not found: a.pdf"),
        ({"status": "error", "message": "Aucun élément"}, "Aucun élément"),
        ({"success": False, "error": "Operation 'INSERT' is not allowed"}, "Operation 'INSERT' is not allowed"),
    ],
)
def test_reported_error_fails_the_node_and_the_run(result, message):
    events = _run([T, _tool(), OUT], EDGES, result)
    err = next(e for e in events if e["event"] == "node_error")
    assert err["node_id"] == "tool"
    assert err["code"] == "tool_reported_error"
    assert message in err["detail"]
    assert events[-1]["event"] == "error"
    assert not any(e["event"] == "node_start" and e["node_id"] == "out" for e in events)


def test_continue_keeps_the_error_result_as_output():
    result = {"status": "error", "message": "boom"}
    events = _run([T, _tool("continue"), OUT], EDGES, result)
    assert not any(e["event"] == "node_error" for e in events)
    assert events[-1] == {"event": "done", "output": result}


@pytest.mark.parametrize(
    "result",
    [
        {"status": "success", "markdown": "ok"},
        {"success": True, "row_count": 1},
        {"status": "errors_reviewed"},
        ["status", "error"],
        "status: error",
        None,
    ],
)
def test_success_shapes_are_untouched(result):
    events = _run([T, _tool(), OUT], EDGES, result)
    assert events[-1]["event"] == "done"


def test_try_catches_a_reported_error():
    body = {
        "nodes": [{"id": "bt", "type": "trigger"}, _tool()],
        "edges": [{"source": "bt", "target": "tool"}],
    }
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
    events = _run(nodes, edges, {"status": "error", "message": "boom"})
    assert events[-1] == {"event": "done", "output": "ko"}


def test_unknown_on_error_is_refused():
    graph = wg.WorkflowGraph.model_validate(
        {"version": 1, "nodes": [T, _tool("ignore"), OUT], "edges": EDGES}
    )
    with pytest.raises(wg.GraphError, match="on_error"):
        wg.validate_graph(graph)

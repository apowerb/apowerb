"""Sortie d'un nœud plafonnée (e2e agent-dev 22/09, lot 5).

Mesuré : une boucle foreach dont le corps convertit son entrée en texte
re-sérialise ``previous`` à chaque tour ; les échappements doublent la taille
à chaque itération (128 o -> 377 Ko en 14 tours, SSE 9,8 Mo en 442 ms), et
``max_iterations`` va jusqu'à 100. Chaque sortie de nœud est donc bornée
comme la réponse du nœud http.
"""

import asyncio
import json

from apowerb.core import workflow_graph as wg

T = {"id": "t", "type": "trigger"}


def _run(nodes, edges, payload=None, agents=None):
    agents = agents or {}

    async def run_agent(agent_id, message):
        return agents.get(agent_id, "x")

    async def run_tool(tool, args):
        return {}

    async def go():
        out = []
        async for chunk in wg.run_graph(
            wg.WorkflowGraph.model_validate(
                {"version": 1, "nodes": nodes, "edges": edges}
            ),
            payload=payload,
            run_agent=run_agent,
            run_tool=run_tool,
            cancel_event=asyncio.Event(),
        ):
            out.append(json.loads(chunk[len("data: ") :]))
        return out

    return asyncio.run(go())


def test_a_node_output_over_the_cap_fails_the_run_with_its_code():
    big = "x" * (wg.MAX_NODE_OUTPUT_BYTES + 1)
    events = _run(
        [T, {"id": "a", "type": "agent", "config": {"agent_id": "big"}}],
        [{"source": "t", "target": "a"}],
        agents={"big": big},
    )
    assert events[-1]["event"] == "error"
    assert events[-1]["code"] == "node_output_too_large"
    assert events[-1]["params"] == {
        "node": "a",
        "max": str(wg.MAX_NODE_OUTPUT_BYTES),
    }
    assert not any(
        e["event"] == "node_complete" and e["node_id"] == "a" for e in events
    )


def test_an_output_at_the_cap_still_passes():
    ok = "x" * (wg.MAX_NODE_OUTPUT_BYTES - 2)  # + 2 guillemets JSON = le plafond
    events = _run(
        [T, {"id": "a", "type": "agent", "config": {"agent_id": "ok"}}],
        [{"source": "t", "target": "a"}],
        agents={"ok": ok},
    )
    assert events[-1]["event"] == "done"


def test_a_loop_reserializing_previous_is_stopped_before_it_explodes():
    body = {
        "version": 1,
        "nodes": [
            {"id": "it", "type": "trigger"},
            {"id": "c", "type": "convert", "config": {"to": "text"}},
        ],
        "edges": [{"source": "it", "target": "c"}],
    }
    events = _run(
        [
            T,
            {
                "id": "l",
                "type": "loop",
                "config": {
                    "mode": "foreach",
                    "items": "{{t.rows}}",
                    "max_iterations": 17,
                    "body": body,
                },
            },
        ],
        [{"source": "t", "target": "l"}],
        payload={"rows": list(range(17))},
    )
    assert events[-1]["event"] == "error"
    assert events[-1]["code"] == "node_output_too_large"
    sizes = [
        len(json.dumps(e["output"], ensure_ascii=False).encode())
        for e in events
        if e["event"] == "node_complete"
    ]
    assert max(sizes) <= wg.MAX_NODE_OUTPUT_BYTES

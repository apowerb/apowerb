"""Plafond du rendu des gabarits (revue de #454, 21/10).

Un gabarit court peut demander une chaîne géante : la même grande sortie
référencée des milliers de fois. La taille d'une sortie n'est vérifiée
qu'après le corps du nœud (``MAX_NODE_OUTPUT_BYTES``) : sans plafond pendant
le rendu, la chaîne était construite en entier avant d'être refusée.
``render`` s'arrête dès que la longueur produite dépasse
``MAX_RENDERED_CHARS`` et lève ``render_too_large`` en nommant le nœud.
"""

import asyncio
import json
import time

import pytest

from apowerb.core import workflow_graph as wg
from apowerb.core.workflow_graph import GraphError


def _events(graph, payload=None):
    async def run_agent(agent_id, message):
        return "x"

    async def run_tool(tool, args):
        return {}

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


def _set_graph(value):
    return {
        "version": 1,
        "nodes": [
            {"id": "start", "type": "trigger"},
            {
                "id": "s",
                "type": "set",
                "config": {"fields": [{"key": "v", "value": value}]},
            },
        ],
        "edges": [{"source": "start", "target": "s"}],
    }


def test_limit_is_above_the_node_output_cap():
    # Un résultat légitime ne dépasse pas la sortie maximale d'un nœud.
    assert wg.MAX_RENDERED_CHARS >= wg.MAX_NODE_OUTPUT_BYTES


def test_amplified_text_stops_early_and_names_the_node():
    big = "x" * 100_000
    t0 = time.perf_counter()
    with pytest.raises(GraphError) as exc:
        wg.render("{{a}}" * 100_000, {"a": big}, "suivant")
    assert time.perf_counter() - t0 < 1.0
    assert exc.value.code == "render_too_large"
    assert exc.value.params == {"node": "suivant", "max": str(wg.MAX_RENDERED_CHARS)}


def test_amplification_through_dicts_and_lists_is_counted():
    big = "y" * (wg.MAX_RENDERED_CHARS // 4)
    template = {"a": ["{{b}}", "{{b}}"], "c": {"d": "{{b}}", "e": "{{b}} et {{b}}"}}
    with pytest.raises(GraphError) as exc:
        wg.render(template, {"b": big}, "n")
    assert exc.value.code == "render_too_large"


def test_whole_value_references_are_counted_too():
    big = {"k": "z" * (wg.MAX_RENDERED_CHARS // 2)}
    with pytest.raises(GraphError):
        wg.render(["{{b}}"] * 4, {"b": big}, "n")


def test_text_just_under_the_limit_renders_in_full():
    chunk = "w" * 10_000
    n = (wg.MAX_RENDERED_CHARS - 100) // len(chunk)
    out = wg.render("{{a}}" * n, {"a": chunk}, "n")
    assert len(out) == n * len(chunk)
    assert out == chunk * n


def test_normal_templates_are_unchanged():
    outputs = {"start": {"client": "Acme", "l": [{"sku": "A1"}]}, "r": "Bonjour"}
    assert (
        wg.render("{{start.client}} {{start.l.0.sku}} {{r}}", outputs)
        == "Acme A1 Bonjour"
    )
    assert wg.render("{{start.l}}", outputs) == [{"sku": "A1"}]
    assert wg.render({"x": ["{{r}}", 3]}, outputs) == {"x": ["Bonjour", 3]}


def test_run_with_amplified_set_node_fails_on_that_node():
    events = _events(_set_graph("{{start.a}}" * 50), payload={"a": "x" * 100_000})
    err = next(e for e in events if e["event"] == "node_error")
    assert err["node_id"] == "s"
    assert err["code"] == "render_too_large"
    assert err["params"]["node"] == "s"
    assert events[-1]["event"] == "error"


def test_run_with_normal_set_node_still_works():
    events = _events(_set_graph("Client {{start.a}}"), payload={"a": "Acme"})
    done = next(
        e for e in events if e["event"] == "node_complete" and e["node_id"] == "s"
    )
    assert done["output"] == {"v": "Client Acme"}

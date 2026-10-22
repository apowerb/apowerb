"""Graphes enregistrés avant la gestion d'erreurs v1 : même issue qu'avant.

Ce fichier ne lit que ce que main produisait déjà (issue du run, code de
l'erreur, sortie) ; il passe à l'identique sur main et sur cette branche.

* ``tool`` + ``on_error: continue`` n'absorbe que l'erreur que l'outil
  signale lui-même dans son résultat ; un outil qui lève ou qui rate son
  contrat (``output_type``) arrête le run.
* ``on_error`` ou ``timeout_s`` posés sur un nœud qui n'en a pas : ignorés,
  le graphe s'exécute (main ne les vérifiait pas).
* ``timeout_s`` sur un agent ou un outil : ignoré, comme sur main — ni l'un
  ni l'autre ne peut être vraiment interrompu.
* Valeur inconnue de ``on_error`` ou ``accept_error_status`` : ignorée (le
  défaut s'applique), pas refusée — main ne lisait pas ces réglages.
"""

from __future__ import annotations

import asyncio
import json

from apowerb.core import workflow_graph as wg

T = {"id": "t", "type": "trigger"}


def _run(nodes, edges, *, tools=None, agents=None):
    graph = wg.WorkflowGraph.model_validate(
        {"version": 1, "nodes": nodes, "edges": edges}
    )
    wg.validate_graph(graph)
    tools, agents = tools or {}, agents or {}

    async def run_agent(agent_id, _message):
        out = agents.get(agent_id, f"out:{agent_id}")
        return (await out()) if callable(out) else out

    async def run_tool(tool, args):
        out = tools.get(tool, {"ok": True})
        return out(args) if callable(out) else out

    async def run_subworkflow(_workflow_id):
        return None

    async def _go():
        out = []
        async for chunk in wg.run_graph(
            graph,
            payload={},
            run_agent=run_agent,
            run_tool=run_tool,
            run_subworkflow=run_subworkflow,
            cancel_event=asyncio.Event(),
        ):
            out.append(json.loads(chunk[len("data: ") :]))
        return out[-1]

    return asyncio.run(_go())


def _tool(**cfg):
    return {"id": "a", "type": "tool", "config": {"tool": "x.y", "args": {}, **cfg}}


EDGE = [{"source": "t", "target": "a"}]


def test_continue_on_a_tool_that_raises_stops_the_run():
    def raising(_args):
        raise wg.GraphError("panne", code="tool_down")

    end = _run([T, _tool(on_error="continue")], EDGE, tools={"x.y": raising})
    assert (end["event"], end.get("code")) == ("error", "tool_down")


def test_continue_on_a_tool_that_breaks_its_contract_stops_the_run():
    end = _run(
        [T, _tool(on_error="continue", output_type="json")],
        EDGE,
        tools={"x.y": "pas du json"},
    )
    assert (end["event"], end.get("code")) == ("error", "output_type_mismatch")


def test_continue_on_a_reported_tool_error_passes_the_tool_result():
    result = {"status": "error", "message": "boom"}
    end = _run([T, _tool(on_error="continue")], EDGE, tools={"x.y": result})
    assert end == {"event": "done", "output": result}


def test_continue_on_a_reported_error_still_checks_the_contract():
    # main : le contrat passait avant l'erreur signalée.
    end = _run(
        [T, _tool(on_error="continue", output_type="text")],
        EDGE,
        tools={"x.y": {"status": "error", "message": "boom"}},
    )
    assert (end["event"], end.get("code")) == ("error", "output_type_mismatch")


def test_a_reported_tool_error_stops_the_run_by_default():
    end = _run([T, _tool()], EDGE, tools={"x.y": {"status": "error", "message": "x"}})
    assert (end["event"], end.get("code")) == ("error", "tool_reported_error")


def test_settings_on_a_node_without_them_are_ignored():
    node = {
        "id": "a",
        "type": "set",
        "config": {
            "fields": [{"key": "k", "value": "v"}],
            "on_error": "continue",
            "timeout_s": 5,
        },
    }
    assert _run([T, node], EDGE) == {"event": "done", "output": {"k": "v"}}


def test_invalid_values_on_a_node_without_them_are_ignored():
    node = {
        "id": "a",
        "type": "set",
        "config": {
            "fields": [{"key": "k", "value": "v"}],
            "on_error": "bogus",
            "timeout_s": "abc",
        },
    }
    assert _run([T, node], EDGE) == {"event": "done", "output": {"k": "v"}}


def test_an_unknown_on_error_behaves_as_the_default():
    # main ne lisait on_error que sur un outil (où il refusait déjà l'inconnu).
    async def boom():
        raise wg.GraphError("panne", code="agent_down")

    agent = {"id": "a", "type": "agent", "config": {"agent_id": "a1", "on_error": "x"}}
    assert _run([T, agent], EDGE)["event"] == "done"
    end = _run([T, agent], EDGE, agents={"a1": boom})
    assert (end["event"], end.get("code")) == ("error", "agent_down")


def test_an_invalid_accept_error_status_does_not_refuse_the_graph():
    http = {
        "id": "a",
        "type": "http",
        "config": {
            "method": "GET",
            "url": "https://example.com/",
            "accept_error_status": "oui",
        },
    }
    wg.validate_graph(
        wg.WorkflowGraph.model_validate(
            {"version": 1, "nodes": [T, http], "edges": EDGE}
        )
    )


def test_a_time_limit_on_an_agent_or_a_tool_is_ignored():
    async def slow():
        await asyncio.sleep(1.2)
        return "fini"

    agent = {"id": "a", "type": "agent", "config": {"agent_id": "a1", "timeout_s": 1}}
    assert _run([T, agent], EDGE, agents={"a1": slow}) == {
        "event": "done",
        "output": "fini",
    }
    assert _run([T, _tool(timeout_s=1)], EDGE)["event"] == "done"

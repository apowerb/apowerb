"""Gabarits ``{{...}}`` : refuser ceux qui ne peuvent jamais se résoudre
(roadmap : Studio insérait ``{{id.output}}``, ``{{router.route}}``,
``{{trigger.payload}}`` — aucun de ces trois champs n'est jamais stocké, voir
``workflow_graph._Compiler._wrap`` et ``_body``).

Décision retenue : le stockage des sorties ne change pas. La référence à une
sortie entière est ``{{id}}`` ; un chemin n'est permis que vers un champ que
le moteur garantit STATIQUEMENT (les sources d'un ``merge``,
``iteration.output``/``iteration.index`` dans un ``until``). ``convert`` vers
texte/nombre/booléen refuse tout chemin à la validation ; ``router`` et
``classifier`` refusent ``.route`` (jamais stocké), mais gardent leurs autres
chemins, forme inconnue comme ``tool``/``trigger``.

``agent`` reste de forme INCONNUE à la validation : ``run_agent_message``
relit sa réponse en JSON quand elle y ressemble, un chemin dessus peut donc
être légitime (``{{agent1.montant}}``). La garde se déplace à l'exécution,
dans ``_lookup`` : un chemin qui traverse une valeur scalaire (texte, nombre,
booléen) lève ``template_ref_invalid`` au lieu de rendre silencieusement
``None``/``""``.
"""

import asyncio
import json

import pytest

from apowerb.core import workflow_graph as wg
from apowerb.core.workflow_engine import error_fields

T = {"id": "t", "type": "trigger"}


def _graph(nodes, edges):
    return wg.WorkflowGraph.model_validate(
        {"version": 1, "nodes": nodes, "edges": edges}
    )


def _invalid(nodes, edges):
    with pytest.raises(wg.GraphError) as info:
        wg.validate_graph(_graph(nodes, edges))
    return error_fields(info.value)


def _valid(nodes, edges):
    wg.validate_graph(_graph(nodes, edges))


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


def _run(graph, env, payload=None):
    async def _go():
        out = []
        async for chunk in wg.run_graph(
            graph,
            payload=payload,
            run_agent=env.run_agent,
            run_tool=env.run_tool,
            cancel_event=asyncio.Event(),
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


# --- Sortie scalaire : agent, convert(text/number/boolean) -----------------


def test_path_onto_an_agent_output_passes_static_validation():
    # la forme de sortie d'un agent n'est connue qu'à l'exécution (texte ou
    # JSON relu par run_agent_message) : la validation ne peut pas la refuser.
    _valid(
        [
            T,
            {"id": "a", "type": "agent", "config": {"agent_id": "agent1"}},
            {"id": "o", "type": "output", "config": {"value": "{{a.output}}"}},
        ],
        [{"source": "t", "target": "a"}, {"source": "a", "target": "o"}],
    )


def test_bare_reference_to_an_agent_output_is_allowed():
    _valid(
        [
            T,
            {"id": "a", "type": "agent", "config": {"agent_id": "agent1"}},
            {"id": "o", "type": "output", "config": {"value": "{{a}}"}},
        ],
        [{"source": "t", "target": "a"}, {"source": "a", "target": "o"}],
    )


@pytest.mark.parametrize("to", ["text", "number", "boolean"])
def test_path_onto_a_scalar_convert_is_refused(to):
    fields = _invalid(
        [
            T,
            {"id": "c", "type": "convert", "config": {"to": to, "input": "{{t}}"}},
            {"id": "o", "type": "output", "config": {"value": "{{c.x}}"}},
        ],
        [{"source": "t", "target": "c"}, {"source": "c", "target": "o"}],
    )
    assert fields["code"] == "template_ref_invalid"
    assert fields["params"] == {"node": "o", "ref": "c.x"}


@pytest.mark.parametrize("to", ["json", "list"])
def test_path_onto_a_json_or_list_convert_is_allowed(to):
    _valid(
        [
            T,
            {"id": "c", "type": "convert", "config": {"to": to, "input": "{{t}}"}},
            {"id": "o", "type": "output", "config": {"value": "{{c.x}}"}},
        ],
        [{"source": "t", "target": "c"}, {"source": "c", "target": "o"}],
    )


# --- Nœud à clés connues : merge --------------------------------------------


def _merge_graph(output_value):
    return (
        [
            T,
            {"id": "a", "type": "agent", "config": {"agent_id": "agent1"}},
            {"id": "b", "type": "agent", "config": {"agent_id": "agent2"}},
            {"id": "m", "type": "merge"},
            {"id": "o", "type": "output", "config": {"value": output_value}},
        ],
        [
            {"source": "t", "target": "a"},
            {"source": "t", "target": "b"},
            {"source": "a", "target": "m"},
            {"source": "b", "target": "m"},
            {"source": "m", "target": "o"},
        ],
    )


def test_path_to_a_real_merge_source_is_allowed():
    nodes, edges = _merge_graph("{{m.a}}")
    _valid(nodes, edges)


def test_path_to_an_unknown_merge_source_is_refused():
    nodes, edges = _merge_graph("{{m.zzz}}")
    fields = _invalid(nodes, edges)
    assert fields["code"] == "template_ref_invalid"
    assert fields["params"] == {"node": "o", "ref": "m.zzz"}


# --- Router / classifier : .route jamais stocké -----------------------------


def test_router_route_path_is_refused():
    fields = _invalid(
        [
            T,
            {
                "id": "r",
                "type": "router",
                "config": {
                    "rules": [
                        {"route": "x", "field": "{{t.v}}", "op": "eq", "value": 1}
                    ],
                    "default_route": "y",
                },
            },
            {"id": "ax", "type": "agent", "config": {"agent_id": "agent1"}},
            {"id": "ay", "type": "agent", "config": {"agent_id": "agent2"}},
            {"id": "o", "type": "output", "config": {"value": "{{r.route}}"}},
        ],
        [
            {"source": "t", "target": "r"},
            {"source": "r", "target": "ax", "route": "x"},
            {"source": "r", "target": "ay", "route": "y"},
            {"source": "ax", "target": "o"},
            {"source": "ay", "target": "o"},
        ],
    )
    assert fields["code"] == "template_ref_invalid"
    assert fields["params"] == {"node": "o", "ref": "r.route"}


def test_classifier_route_path_is_refused():
    fields = _invalid(
        [
            T,
            {
                "id": "c",
                "type": "classifier",
                "config": {
                    "agent_id": "agent1",
                    "routes": [{"route": "x"}, {"route": "y"}],
                },
            },
            {"id": "ax", "type": "agent", "config": {"agent_id": "agent2"}},
            {"id": "ay", "type": "agent", "config": {"agent_id": "agent3"}},
            {"id": "o", "type": "output", "config": {"value": "{{c.route}}"}},
        ],
        [
            {"source": "t", "target": "c"},
            {"source": "c", "target": "ax", "route": "x"},
            {"source": "c", "target": "ay", "route": "y"},
            {"source": "ax", "target": "o"},
            {"source": "ay", "target": "o"},
        ],
    )
    assert fields["code"] == "template_ref_invalid"
    assert fields["params"] == {"node": "o", "ref": "c.route"}


def test_router_passthrough_path_other_than_route_stays_allowed():
    # forme inconnue (passthrough de l'entrée) : un chemin réel y reste permis.
    _valid(
        [
            T,
            {
                "id": "r",
                "type": "router",
                "config": {
                    "rules": [
                        {"route": "x", "field": "{{t.v}}", "op": "eq", "value": 1}
                    ],
                    "default_route": "y",
                },
            },
            {"id": "ax", "type": "agent", "config": {"agent_id": "agent1"}},
            {"id": "o", "type": "output", "config": {"value": "{{r.anything}}"}},
        ],
        [
            {"source": "t", "target": "r"},
            {"source": "r", "target": "ax", "route": "x"},
            {"source": "ax", "target": "o"},
        ],
    )


# --- trigger / tool : forme inconnue, chemins non restreints ---------------


def test_trigger_and_tool_paths_stay_unrestricted():
    _valid(
        [
            T,
            {
                "id": "w",
                "type": "tool",
                "config": {"tool": "any.tool", "args": {"x": "{{t.whatever}}"}},
            },
            {"id": "o", "type": "output", "config": {"value": "{{w.whatever}}"}},
        ],
        [{"source": "t", "target": "w"}, {"source": "w", "target": "o"}],
    )


# --- iteration (corps de boucle, condition until) ---------------------------

_UNTIL_BODY = {
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


def test_iteration_output_and_index_paths_are_allowed():
    _valid(
        [
            T,
            {
                "id": "l",
                "type": "loop",
                "config": {
                    "mode": "until",
                    "max_iterations": 3,
                    "body": _UNTIL_BODY,
                    "until": {
                        "field": "{{iteration.output.n}}",
                        "op": "gte",
                        "value": 3,
                    },
                },
            },
        ],
        [{"source": "t", "target": "l"}],
    )
    _valid(
        [
            T,
            {
                "id": "l",
                "type": "loop",
                "config": {
                    "mode": "until",
                    "max_iterations": 3,
                    "body": _UNTIL_BODY,
                    "until": {"field": "{{iteration.index}}", "op": "gte", "value": 2},
                },
            },
        ],
        [{"source": "t", "target": "l"}],
    )


def test_iteration_unknown_field_is_refused():
    fields = _invalid(
        [
            T,
            {
                "id": "l",
                "type": "loop",
                "config": {
                    "mode": "until",
                    "max_iterations": 3,
                    "body": _UNTIL_BODY,
                    "until": {"field": "{{iteration.zzz}}", "op": "eq", "value": 1},
                },
            },
        ],
        [{"source": "t", "target": "l"}],
    )
    assert fields["code"] == "template_ref_invalid"
    assert fields["params"] == {"node": "l", "ref": "iteration.zzz"}


def test_a_bad_path_inside_a_loop_body_is_refused():
    body = {
        "version": 1,
        "nodes": [
            {"id": "it", "type": "trigger"},
            {"id": "w", "type": "convert", "config": {"to": "text", "input": "{{it}}"}},
            {"id": "bo", "type": "output", "config": {"value": "{{w.x}}"}},
        ],
        "edges": [{"source": "it", "target": "w"}, {"source": "w", "target": "bo"}],
    }
    with pytest.raises(wg.GraphError) as info:
        wg.validate_graph(
            _graph(
                [
                    T,
                    {
                        "id": "l",
                        "type": "loop",
                        "config": {
                            "mode": "foreach",
                            "items": "{{t.rows}}",
                            "max_iterations": 3,
                            "body": body,
                        },
                    },
                ],
                [{"source": "t", "target": "l"}],
            )
        )
    assert "w.x" in str(info.value)


# --- agent : forme inconnue à la validation, garde à l'exécution -----------


def test_agent_dotted_path_fails_at_runtime_on_scalar_text_output():
    env = _Env(agents={"agent1": lambda m: "bonjour"})
    g = _graph(
        [
            T,
            {"id": "agent1", "type": "agent", "config": {"agent_id": "agent1"}},
            {"id": "o", "type": "output", "config": {"value": "{{agent1.output}}"}},
        ],
        [{"source": "t", "target": "agent1"}, {"source": "agent1", "target": "o"}],
    )
    events = _run(g, env)
    err = _node_error(events, "o")
    assert err["code"] == "template_ref_invalid"
    assert err["params"] == {"node": "o", "ref": "agent1.output"}
    assert events[-1]["event"] == "error"
    assert events[-1]["code"] == "template_ref_invalid"


def test_agent_dotted_path_resolves_a_real_json_field_at_runtime():
    env = _Env(agents={"agent1": lambda m: {"montant": 42}})
    g = _graph(
        [
            T,
            {"id": "agent1", "type": "agent", "config": {"agent_id": "agent1"}},
            {"id": "o", "type": "output", "config": {"value": "{{agent1.montant}}"}},
        ],
        [{"source": "t", "target": "agent1"}, {"source": "agent1", "target": "o"}],
    )
    assert _done(_run(g, env)) == 42


def test_agent_missing_branch_still_renders_none_not_an_error():
    # un nœud absent des sorties (branche de routeur non prise) reste None
    # (référence nue non résolue) ; seule une valeur scalaire réellement
    # traversée est une erreur.
    env = _Env(agents={"agent1": lambda m: "branch one"})
    g = _graph(
        [
            T,
            {
                "id": "r",
                "type": "router",
                "config": {
                    "rules": [
                        {"route": "one", "field": "{{t.pick}}", "op": "eq", "value": 1}
                    ],
                    "default_route": "two",
                },
            },
            {"id": "agent1", "type": "agent", "config": {"agent_id": "agent1"}},
            {"id": "agent2", "type": "agent", "config": {"agent_id": "agent2"}},
            {"id": "o", "type": "output", "config": {"value": "{{agent2.output}}"}},
        ],
        [
            {"source": "t", "target": "r"},
            {"source": "r", "target": "agent1", "route": "one"},
            {"source": "r", "target": "agent2", "route": "two"},
            {"source": "agent1", "target": "o"},
            {"source": "agent2", "target": "o"},
        ],
    )
    assert _done(_run(g, env, payload={"pick": 1})) is None


# --- Router à branches : Output s'exécute avec une seule branche prise -----


def test_output_after_a_router_runs_even_when_only_one_branch_executes():
    env = _Env(
        agents={"agent1": lambda m: "branch one", "agent2": lambda m: "branch two"}
    )
    g = _graph(
        [
            T,
            {
                "id": "r",
                "type": "router",
                "config": {
                    "rules": [
                        {"route": "one", "field": "{{t.pick}}", "op": "eq", "value": 1}
                    ],
                    "default_route": "two",
                },
            },
            {"id": "agent1", "type": "agent", "config": {"agent_id": "agent1"}},
            {"id": "agent2", "type": "agent", "config": {"agent_id": "agent2"}},
            {"id": "o", "type": "output", "config": {"value": "{{agent1}}{{agent2}}"}},
        ],
        [
            {"source": "t", "target": "r"},
            {"source": "r", "target": "agent1", "route": "one"},
            {"source": "r", "target": "agent2", "route": "two"},
            {"source": "agent1", "target": "o"},
            {"source": "agent2", "target": "o"},
        ],
    )
    events = _run(g, env, payload={"pick": 1})
    assert _done(events) == "branch one"


# --- Bout en bout : {{agent1}} vs {{agent1.output}} -------------------------


def test_end_to_end_bare_reference_returns_the_agent_text():
    async def run_agent(agent_id, message):
        return "bonjour"

    async def run_tool(tool, args):  # pragma: no cover - non utilisé ici
        raise AssertionError("aucun outil attendu")

    graph = _graph(
        [
            T,
            {"id": "agent1", "type": "agent", "config": {"agent_id": "agent1"}},
            {"id": "o", "type": "output", "config": {"value": "{{agent1}}"}},
        ],
        [{"source": "t", "target": "agent1"}, {"source": "agent1", "target": "o"}],
    )

    async def _go():
        out = []
        async for chunk in wg.run_graph(
            graph,
            payload=None,
            run_agent=run_agent,
            run_tool=run_tool,
            cancel_event=asyncio.Event(),
        ):
            out.append(json.loads(chunk[len("data: ") :]))
        return out

    events = asyncio.run(_go())
    assert events[-1]["event"] == "done"
    assert events[-1]["output"] == "bonjour"


def test_end_to_end_dotted_reference_passes_validation_then_fails_at_runtime():
    async def run_agent(agent_id, message):
        return "bonjour"

    async def run_tool(tool, args):  # pragma: no cover - non utilisé ici
        raise AssertionError("aucun outil attendu")

    graph = _graph(
        [
            T,
            {"id": "agent1", "type": "agent", "config": {"agent_id": "agent1"}},
            {"id": "o", "type": "output", "config": {"value": "{{agent1.output}}"}},
        ],
        [{"source": "t", "target": "agent1"}, {"source": "agent1", "target": "o"}],
    )
    wg.validate_graph(graph)  # ne lève pas : agent est de forme inconnue

    async def _go():
        out = []
        async for chunk in wg.run_graph(
            graph,
            payload=None,
            run_agent=run_agent,
            run_tool=run_tool,
            cancel_event=asyncio.Event(),
        ):
            out.append(json.loads(chunk[len("data: ") :]))
        return out

    events = asyncio.run(_go())
    err = _node_error(events, "o")
    assert err["code"] == "template_ref_invalid"
    assert err["params"] == {"node": "o", "ref": "agent1.output"}
    assert events[-1]["event"] == "error"
    assert events[-1]["code"] == "template_ref_invalid"


def test_end_to_end_dotted_reference_resolves_a_real_json_field():
    async def run_agent(agent_id, message):
        return {"montant": 42}

    async def run_tool(tool, args):  # pragma: no cover - non utilisé ici
        raise AssertionError("aucun outil attendu")

    graph = _graph(
        [
            T,
            {"id": "agent1", "type": "agent", "config": {"agent_id": "agent1"}},
            {"id": "o", "type": "output", "config": {"value": "{{agent1.montant}}"}},
        ],
        [{"source": "t", "target": "agent1"}, {"source": "agent1", "target": "o"}],
    )

    async def _go():
        out = []
        async for chunk in wg.run_graph(
            graph,
            payload=None,
            run_agent=run_agent,
            run_tool=run_tool,
            cancel_event=asyncio.Event(),
        ):
            out.append(json.loads(chunk[len("data: ") :]))
        return out

    events = asyncio.run(_go())
    assert events[-1]["event"] == "done"
    assert events[-1]["output"] == 42

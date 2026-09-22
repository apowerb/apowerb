"""Nœuds Set et Condition (LOT 1, 21/09).

Set construit un dict de sortie à partir de gabarits, sans exécuter de code
utilisateur : il remplace le besoin exprimé pour un nœud « Transform ».
Condition réutilise exactement les opérateurs et l'évaluation des règles du
routeur (``evaluate_rule``), mais route sur deux issues fixes
``"true"``/``"false"`` et renvoie son entrée inchangée.
"""

import asyncio
import json

import pytest

from apowerb.core import workflow_graph as wg


def _run(nodes, edges, payload=None):
    async def run_agent(agent_id, message):
        return f"out:{agent_id}"

    async def run_tool(tool, args):
        return {"tool": tool}

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


T = {"id": "t", "type": "trigger"}


# --- set ---------------------------------------------------------------


def test_set_node_renders_each_field_with_the_same_template_engine_in_order():
    events = _run(
        [
            T,
            {
                "id": "s",
                "type": "set",
                "config": {
                    "fields": [
                        {"key": "greeting", "value": "hello {{t.name}}"},
                        {"key": "raw", "value": "{{t.name}}"},
                        {"key": "tags", "value": ["{{t.name}}", "static"]},
                        {"key": "meta", "value": {"n": "{{t.name}}"}},
                        {"key": "a.b", "value": 1},
                    ]
                },
            },
        ],
        [{"source": "t", "target": "s"}],
        payload={"name": "Elom"},
    )
    assert events[-1]["output"] == {
        "greeting": "hello Elom",
        "raw": "Elom",
        "tags": ["Elom", "static"],
        "meta": {"n": "Elom"},
        "a.b": 1,
    }
    # Ordre des champs préservé, "a.b" est une clé plate (pas un chemin).
    assert list(events[-1]["output"].keys()) == [
        "greeting",
        "raw",
        "tags",
        "meta",
        "a.b",
    ]


def test_set_node_only_reads_upstream_nodes():
    with pytest.raises(wg.GraphError, match="nope"):
        wg.validate_graph(
            wg.WorkflowGraph.model_validate(
                {
                    "version": 1,
                    "nodes": [
                        T,
                        {
                            "id": "s",
                            "type": "set",
                            "config": {"fields": [{"key": "x", "value": "{{nope.y}}"}]},
                        },
                    ],
                    "edges": [{"source": "t", "target": "s"}],
                }
            )
        )


@pytest.mark.parametrize(
    "fields,needle",
    [
        (None, "champ"),
        ([], "champ"),
        ([{"key": "", "value": 1}], "clé"),
        ([{"key": "   ", "value": 1}], "clé"),
        ([{"key": "x", "value": 1}, {"key": "x", "value": 2}], "dupl"),
    ],
)
def test_set_node_validation(fields, needle):
    cfg = {"fields": fields} if fields is not None else {}
    with pytest.raises(wg.GraphError, match=needle):
        wg.validate_graph(
            wg.WorkflowGraph.model_validate(
                {
                    "version": 1,
                    "nodes": [T, {"id": "s", "type": "set", "config": cfg}],
                    "edges": [{"source": "t", "target": "s"}],
                }
            )
        )


# --- condition -----------------------------------------------------------


def _condition_graph(rules, match, edges_extra=None):
    nodes = [
        T,
        {"id": "c", "type": "condition", "config": {"rules": rules, "match": match}},
        {"id": "yes", "type": "agent", "config": {"agent_id": "y"}},
        {"id": "no", "type": "agent", "config": {"agent_id": "n"}},
    ]
    edges = [{"source": "t", "target": "c"}] + (
        edges_extra
        if edges_extra is not None
        else [
            {"source": "c", "target": "yes", "route": "true"},
            {"source": "c", "target": "no", "route": "false"},
        ]
    )
    return nodes, edges


def test_condition_all_routes_true_only_when_every_rule_passes():
    nodes, edges = _condition_graph(
        [
            {"field": "{{t.age}}", "op": "gte", "value": 18},
            {"field": "{{t.country}}", "op": "eq", "value": "FR"},
        ],
        "all",
    )
    events = _run(nodes, edges, payload={"age": 20, "country": "FR"})
    route = next(e for e in events if e["event"] == "route")
    assert route["route"] == "true"
    assert events[-1]["output"] == "out:y"

    events = _run(nodes, edges, payload={"age": 20, "country": "BE"})
    route = next(e for e in events if e["event"] == "route")
    assert route["route"] == "false"
    assert events[-1]["output"] == "out:n"


def test_condition_any_routes_true_when_one_rule_passes():
    nodes, edges = _condition_graph(
        [
            {"field": "{{t.vip}}", "op": "eq", "value": True},
            {"field": "{{t.amount}}", "op": "gt", "value": 1000},
        ],
        "any",
    )
    events = _run(nodes, edges, payload={"vip": False, "amount": 5})
    route = next(e for e in events if e["event"] == "route")
    assert route["route"] == "false"

    events = _run(nodes, edges, payload={"vip": False, "amount": 5000})
    route = next(e for e in events if e["event"] == "route")
    assert route["route"] == "true"


def test_condition_output_is_its_input_unchanged():
    nodes, edges = _condition_graph(
        [{"field": "{{t.x}}", "op": "eq", "value": 1}], "all"
    )
    events = _run(nodes, edges, payload={"x": 1})
    complete = next(
        e for e in events if e["event"] == "node_complete" and e["node_id"] == "c"
    )
    assert complete["output"] == {"x": 1}


def test_condition_defaults_to_match_all():
    nodes, edges = _condition_graph(
        [{"field": "{{t.x}}", "op": "eq", "value": 1}], "all"
    )
    nodes[1]["config"].pop("match")  # absent -> défaut "all"
    events = _run(nodes, edges, payload={"x": 1})
    route = next(e for e in events if e["event"] == "route")
    assert route["route"] == "true"


def test_condition_ends_the_run_on_an_unwired_route_like_a_router():
    events = _run(
        [
            T,
            {
                "id": "c",
                "type": "condition",
                "config": {
                    "rules": [{"field": "{{t.x}}", "op": "eq", "value": 1}],
                    "match": "all",
                },
            },
            {"id": "yes", "type": "agent", "config": {"agent_id": "y"}},
        ],
        [
            {"source": "t", "target": "c"},
            {"source": "c", "target": "yes", "route": "true"},
        ],
        payload={"x": 0},
    )
    assert events[-1] == {"event": "done", "output": {}}
    assert not any(e["event"] == "node_start" and e["node_id"] == "yes" for e in events)


@pytest.mark.parametrize(
    "rules,match,needle",
    [
        (None, "all", "règle"),
        ([], "all", "règle"),
        ([{"field": "{{t.x}}", "op": "eq", "value": 1}], "maybe", "match"),
    ],
)
def test_condition_node_validation(rules, match, needle):
    cfg = {"match": match}
    if rules is not None:
        cfg["rules"] = rules
    with pytest.raises(wg.GraphError, match=needle):
        wg.validate_graph(
            wg.WorkflowGraph.model_validate(
                {
                    "version": 1,
                    "nodes": [T, {"id": "c", "type": "condition", "config": cfg}],
                    "edges": [],
                }
            )
        )


def test_condition_edge_without_a_route_is_refused():
    nodes, edges = _condition_graph(
        [{"field": "{{t.x}}", "op": "eq", "value": 1}],
        "all",
        edges_extra=[{"source": "c", "target": "yes"}],
    )
    with pytest.raises(wg.GraphError, match="route"):
        wg.validate_graph(
            wg.WorkflowGraph.model_validate(
                {"version": 1, "nodes": nodes, "edges": edges}
            )
        )


def test_condition_edge_with_another_route_is_refused():
    nodes, edges = _condition_graph(
        [{"field": "{{t.x}}", "op": "eq", "value": 1}],
        "all",
        edges_extra=[{"source": "c", "target": "yes", "route": "maybe"}],
    )
    with pytest.raises(wg.GraphError, match="route"):
        wg.validate_graph(
            wg.WorkflowGraph.model_validate(
                {"version": 1, "nodes": nodes, "edges": edges}
            )
        )


def test_condition_allows_at_most_one_edge_per_route():
    nodes, edges = _condition_graph(
        [{"field": "{{t.x}}", "op": "eq", "value": 1}],
        "all",
        edges_extra=[
            {"source": "c", "target": "yes", "route": "true"},
            {"source": "c", "target": "no", "route": "true"},
        ],
    )
    with pytest.raises(wg.GraphError, match="route"):
        wg.validate_graph(
            wg.WorkflowGraph.model_validate(
                {"version": 1, "nodes": nodes, "edges": edges}
            )
        )


@pytest.mark.parametrize("field", ["", "  ", None])
def test_condition_rule_with_an_empty_field_tests_the_node_input(field):
    """Comme le routeur (e2e agent-dev 22/09) : un champ vide rendait ``""``
    et la condition sortait toujours ``false``, sans erreur."""
    nodes, edges = _condition_graph(
        [{"field": field, "op": "eq", "value": "doc"}], "all"
    )
    events = _run(nodes, edges, payload="doc")
    route = next(e for e in events if e["event"] == "route")
    assert route["route"] == "true"

"""Ce que chaque nœud promet de rendre, et ce que le run montre quand il ne tient pas.

Retours du 21/10/26 sur le Studio : on ne pouvait ni dire qu'un agent devait
rendre du JSON (une réponse en prose passait en silence), ni voir le graphique
qu'un outil venait de créer, ni savoir pourquoi un nœud « validation humaine »
refusait le run.
"""

import asyncio
import json

import pytest

from apowerb.core import workflow_graph as wg

PAYLOAD = {"message": "Analyse les ventes"}


def _graph(nodes, edges):
    return wg.WorkflowGraph.model_validate(
        {"version": 1, "nodes": nodes, "edges": edges}
    )


def _one(node):
    return _graph(
        [{"id": "t", "type": "trigger", "config": {"kind": "manual"}}, node],
        [{"source": "t", "target": node["id"]}],
    )


def _run(graph, *, agent=None, tool=None):
    async def run_agent(agent_id, message):
        return agent(agent_id, message) if agent else "ok"

    async def run_tool(name, args):
        return tool(name, args) if tool else {}

    async def _go():
        out = []
        async for chunk in wg.run_graph(
            graph,
            payload=PAYLOAD,
            run_agent=run_agent,
            run_tool=run_tool,
            cancel_event=asyncio.Event(),
        ):
            out.append(json.loads(chunk[len("data: ") :]))
        return out

    return asyncio.run(_go())


def _ev(events, kind, node_id=None):
    return [
        e
        for e in events
        if e["event"] == kind and (node_id is None or e.get("node_id") == node_id)
    ]


# --- Type de sortie attendu --------------------------------------------------


def test_agent_expected_json_but_got_prose_fails_with_a_code():
    events = _run(
        _one(
            {
                "id": "a",
                "type": "agent",
                "config": {"agent_id": "x", "output_type": "json"},
            }
        ),
        agent=lambda *_: "Voici l'analyse : les ventes montent.",
    )
    (err,) = _ev(events, "node_error", "a")
    assert err["code"] == "output_type_mismatch"
    assert err["params"] == {"node": "a", "expected": "json", "actual": "text"}
    assert events[-1]["event"] == "error"


def test_agent_expected_json_and_got_json_passes():
    events = _run(
        _one(
            {
                "id": "a",
                "type": "agent",
                "config": {"agent_id": "x", "output_type": "json"},
            }
        ),
        agent=lambda *_: {"total": 3},
    )
    assert events[-1] == {"event": "done", "output": {"total": 3}}


def test_expected_text_refuses_json():
    events = _run(
        _one(
            {
                "id": "a",
                "type": "agent",
                "config": {"agent_id": "x", "output_type": "text"},
            }
        ),
        agent=lambda *_: {"total": 3},
    )
    (err,) = _ev(events, "node_error", "a")
    assert err["params"]["actual"] == "json"


def test_a_number_is_neither_text_nor_json():
    events = _run(
        _one(
            {"id": "o", "type": "tool", "config": {"tool": "t1", "output_type": "text"}}
        ),
        tool=lambda *_: 42,
    )
    (err,) = _ev(events, "node_error", "o")
    assert err["params"] == {"node": "o", "expected": "text", "actual": "int"}


def test_unknown_question_policy_has_a_code():
    with pytest.raises(wg.GraphError) as exc:
        wg.validate_graph(
            _one(
                {
                    "id": "a",
                    "type": "agent",
                    "config": {"agent_id": "x", "on_question": "ask"},
                }
            )
        )
    assert exc.value.code == "on_question_unknown"


def test_tool_node_can_expect_json_too():
    events = _run(
        _one(
            {"id": "o", "type": "tool", "config": {"tool": "t1", "output_type": "json"}}
        ),
        tool=lambda *_: "texte",
    )
    (err,) = _ev(events, "node_error", "o")
    assert err["code"] == "output_type_mismatch"


def test_no_output_type_keeps_todays_behaviour():
    events = _run(
        _one({"id": "a", "type": "agent", "config": {"agent_id": "x"}}),
        agent=lambda *_: "prose",
    )
    assert events[-1] == {"event": "done", "output": "prose"}


def test_unknown_output_type_is_refused_before_running():
    with pytest.raises(wg.GraphError) as exc:
        wg.validate_graph(
            _one(
                {
                    "id": "a",
                    "type": "agent",
                    "config": {"agent_id": "x", "output_type": "chart"},
                }
            )
        )
    assert exc.value.code == "output_type_unknown"


# --- Agent qui pose une question ---------------------------------------------


def test_agent_can_be_told_to_fail_when_it_asks_a_question():
    events = _run(
        _one(
            {
                "id": "a",
                "type": "agent",
                "config": {"agent_id": "x", "on_question": "fail"},
            }
        ),
        agent=lambda *_: "La table manque.\nQuelle période voulez-vous ?",
    )
    (err,) = _ev(events, "node_error", "a")
    assert err["code"] == "agent_asked_question"
    assert err["params"]["node"] == "a"
    assert "Quelle période" in err["params"]["question"]


def test_question_without_the_option_still_continues():
    events = _run(
        _one({"id": "a", "type": "agent", "config": {"agent_id": "x"}}),
        agent=lambda *_: "Quelle période ?",
    )
    assert events[-1]["event"] == "done"


# --- Graphiques et tableaux de bord créés par un nœud tool -------------------


def test_tool_node_reports_the_chart_it_created():
    events = _run(
        _one({"id": "o", "type": "tool", "config": {"tool": "tool_create_chart"}}),
        tool=lambda *_: {"success": True, "chart_id": "c42", "title": "Ventes"},
    )
    (done,) = _ev(events, "node_complete", "o")
    assert done["artifacts"] == [{"tool": "tool_create_chart", "chart_id": "c42"}]


def test_failed_tool_reports_no_artifact():
    events = _run(
        _one(
            {
                "id": "o",
                "type": "tool",
                "config": {"tool": "tool_create_chart", "on_error": "continue"},
            }
        ),
        tool=lambda *_: {"success": False, "chart_id": "c42"},
    )
    (done,) = _ev(events, "node_complete", "o")
    assert "artifacts" not in done


def test_dataset_only_read_is_not_an_artifact():
    """``tool_describe_dataset`` rend un ``dataset_id`` qu'il n'a pas créé."""
    events = _run(
        _one({"id": "o", "type": "tool", "config": {"tool": "tool_describe_dataset"}}),
        tool=lambda *_: {"dataset_id": "d1", "columns": []},
    )
    (done,) = _ev(events, "node_complete", "o")
    assert "artifacts" not in done


def test_done_lists_every_artifact_of_the_run():
    events = _run(
        _one({"id": "o", "type": "tool", "config": {"tool": "tool_create_chart"}}),
        tool=lambda *_: {"chart_id": "c42"},
    )
    assert events[-1]["artifacts"] == [
        {"node_id": "o", "tool": "tool_create_chart", "chart_id": "c42"}
    ]


# --- Tool en mode continue ---------------------------------------------------


def test_tool_error_in_continue_mode_is_marked():
    events = _run(
        _one(
            {
                "id": "o",
                "type": "tool",
                "config": {"tool": "t1", "on_error": "continue"},
            }
        ),
        tool=lambda *_: {"success": False, "error": "table introuvable"},
    )
    (done,) = _ev(events, "node_complete", "o")
    assert done["reported_error"] == "table introuvable"


# --- Validation humaine (approval) -------------------------------------------


def test_approval_is_refused_with_a_code_the_studio_can_explain():
    graph = _one({"id": "h", "type": "approval", "config": {}})
    with pytest.raises(wg.GraphError) as exc:
        wg.validate_graph(graph)
    assert exc.value.code == "node_not_runnable"
    assert exc.value.params == {"node": "h", "type": "approval"}
    (err,) = _run(graph)
    assert err["code"] == "invalid_graph"
    assert err["cause"] == "node_not_runnable"
    assert err["params"] == {"node": "h", "type": "approval"}


# --- Le nœud fautif est nommé ------------------------------------------------


@pytest.mark.parametrize(
    "node",
    [
        {
            "id": "s",
            "type": "set",
            "config": {"fields": [{"key": "k", "value": "{{a.nom}}"}]},
        },
        {
            "id": "s",
            "type": "condition",
            "config": {"rules": [{"field": "{{a.nom}}", "op": "equals", "value": "x"}]},
        },
        {
            "id": "s",
            "type": "notification",
            "config": {
                "channel": "email",
                "to": ["x@y.fr"],
                "subject": "{{a.nom}}",
                "body": "b",
            },
        },
    ],
)
def test_bad_reference_names_the_node_that_holds_it(node):
    graph = _graph(
        [
            {"id": "t", "type": "trigger", "config": {"kind": "manual"}},
            {"id": "a", "type": "agent", "config": {"agent_id": "x"}},
            node,
        ],
        [
            {"source": "t", "target": "a"},
            {"source": "a", "target": "s"},
        ],
    )
    events = _run(graph, agent=lambda *_: "texte simple")
    (err,) = _ev(events, "node_error", "s")
    assert err["code"] == "template_ref_invalid"
    assert err["params"]["node"] == "s"

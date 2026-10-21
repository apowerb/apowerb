"""Nœud « Décision humaine » (``approval``) : le run s'arrête, puis reprend.

Le moteur ne sait pas suspendre une tâche ADK : un run qui atteint une
décision s'arrête sur ``awaiting_input`` en confiant son instantané à
``on_await`` (sorties ET routes déjà prises), puis ``run_graph(resume=...)``
rejoue les nœuds faits sans les réexécuter et reprend avec la réponse.
"""

import asyncio
import json

import pytest

from apowerb.core import workflow_graph as wg


def _graph(nodes, edges):
    return wg.WorkflowGraph.model_validate({"version": 1, "nodes": nodes, "edges": edges})


class _Env:
    def __init__(self):
        self.calls = []

    async def run_agent(self, agent_id, message):
        self.calls.append((agent_id, message))
        return f"out:{agent_id}"

    async def run_tool(self, tool, args):
        self.calls.append((tool, args))
        return {"amount": 5000}


def _run(graph, env, *, payload=None, resume=None, saved=None, run_id="r1"):
    async def _on_await(snapshot):
        saved.append(snapshot)

    async def _go():
        out = []
        async for chunk in wg.run_graph(
            graph,
            payload=payload,
            run_agent=env.run_agent,
            run_tool=env.run_tool,
            cancel_event=asyncio.Event(),
            on_await=_on_await if saved is not None else None,
            resume=resume,
            run_id=run_id,
        ):
            out.append(json.loads(chunk[len("data: "):]))
        return out

    return asyncio.run(_go())


def _decision(mode="choices", **extra):
    cfg = {"question": "Valider ? {{a}}", "mode": mode}
    if mode != "text":
        cfg["choices"] = [{"route": "oui", "label": "Oui"}, {"route": "non", "label": "Non"}]
    cfg.update(extra)
    return {"id": "d", "type": "approval", "config": cfg}


NODES = [
    {"id": "t", "type": "trigger", "config": {"kind": "manual"}},
    {"id": "a", "type": "agent", "config": {"agent_id": "agent1"}},
    _decision(),
    {"id": "yes", "type": "output", "config": {"value": "ok:{{d.choice}}"}},
    {"id": "no", "type": "output", "config": {"value": "refus:{{d.text}}"}},
]
EDGES = [
    {"source": "t", "target": "a"},
    {"source": "a", "target": "d"},
    {"source": "d", "target": "yes", "route": "oui"},
    {"source": "d", "target": "no", "route": "non"},
]


def _answer(choice=None, text=None):
    return {
        "node_id": "d",
        "choice": choice,
        "text": text,
        "answered_by": "u@example.com",
        "answered_at": "2026-10-21T18:00:00+00:00",
    }


def _resume(snapshot, **answer):
    return {**snapshot, "answer": _answer(**answer)}


# --- Validation ----------------------------------------------------------


def _code(nodes, edges):
    with pytest.raises(wg.GraphError) as exc:
        wg.validate_graph(_graph(nodes, edges))
    return exc.value.code


def test_a_decision_node_is_runnable_now():
    wg.validate_graph(_graph(NODES, EDGES))


@pytest.mark.parametrize(
    "cfg, code",
    [
        ({"question": " ", "mode": "text"}, "approval_question_missing"),
        ({"question": "Q", "mode": "vote"}, "approval_mode_unknown"),
        ({"question": "Q", "mode": "choices", "choices": []}, "approval_choices_missing"),
        (
            {"question": "Q", "choices": [{"route": "text", "label": "Autre"}]},
            "approval_choice_reserved",
        ),
        (
            {"question": "Q", "choices": [{"route": "x", "label": "A"}, {"route": "x", "label": "B"}]},
            "approval_choice_duplicate",
        ),
    ],
)
def test_a_decision_config_is_checked(cfg, code):
    nodes = [NODES[0], {"id": "d", "type": "approval", "config": cfg}]
    assert _code(nodes, [{"source": "t", "target": "d"}]) == code


def test_both_mode_needs_the_reserved_text_route_wired():
    nodes = NODES[:2] + [_decision("both")] + NODES[3:]
    assert _code(nodes, EDGES) == "approval_text_route_missing"
    wg.validate_graph(_graph(nodes, EDGES + [{"source": "d", "target": "no", "route": "text"}]))


def test_text_mode_is_not_routed():
    nodes = NODES[:2] + [_decision("text")] + NODES[3:4]
    with pytest.raises(wg.GraphError):
        wg.validate_graph(_graph(nodes, EDGES[:2] + [{"source": "d", "target": "yes", "route": "oui"}]))
    wg.validate_graph(_graph(nodes, EDGES[:2] + [{"source": "d", "target": "yes"}]))


def test_a_route_not_declared_by_the_decision_is_refused():
    with pytest.raises(wg.GraphError):
        wg.validate_graph(_graph(NODES, EDGES + [{"source": "d", "target": "no", "route": "peut-etre"}]))


def test_a_decision_inside_a_loop_body_is_refused():
    body = {
        "version": 1,
        "nodes": [{"id": "bt", "type": "trigger", "config": {}}, _decision("text", question="Q")],
        "edges": [{"source": "bt", "target": "d"}],
    }
    loop = {
        "id": "l",
        "type": "loop",
        "config": {"mode": "foreach", "items": "{{t.items}}", "max_iterations": 3, "body": body},
    }
    assert "approval_nested" in str(
        pytest.raises(wg.GraphError, wg.validate_graph, _graph([NODES[0], loop], [{"source": "t", "target": "l"}])).value.code
    )


def test_decision_fields_are_known_to_templates():
    bad = NODES[:4] + [{"id": "no", "type": "output", "config": {"value": "{{d.verdict}}"}}]
    assert _code(bad, EDGES) == "template_ref_invalid"


# --- Pause ------------------------------------------------------------------


def test_the_run_stops_on_the_decision_and_hands_over_its_state():
    env, saved = _Env(), []
    events = _run(_graph(NODES, EDGES), env, saved=saved)
    last = events[-1]
    assert last["event"] == "awaiting_input", events
    assert last["run_id"] == "r1" and last["node_id"] == "d"
    assert last["question"] == "Valider ? out:agent1"
    assert last["mode"] == "choices"
    assert last["choices"] == [{"route": "oui", "label": "Oui"}, {"route": "non", "label": "Non"}]
    assert not [e for e in events if e["event"] in ("node_error", "error", "done")]
    assert not [e for e in events if e.get("node_id") in ("yes", "no")]
    [snap] = saved
    assert snap["node_id"] == "d"
    assert snap["outputs"]["a"] == "out:agent1"
    assert "d" not in snap["outputs"]
    assert snap["prompt"]["question"] == "Valider ? out:agent1"
    assert snap["graph"]["nodes"][2]["id"] == "d"


def test_without_a_place_to_save_the_pause_the_run_fails_clearly():
    events = _run(_graph(NODES, EDGES), _Env())
    assert events[-1]["event"] == "error"
    assert events[-1]["code"] == "decision_unavailable"


def test_a_pause_that_cannot_be_saved_is_an_error_not_a_lost_run():
    async def _broken(_snapshot):
        raise RuntimeError("db down")

    async def _go():
        env = _Env()
        return [
            json.loads(c[6:])
            async for c in wg.run_graph(
                _graph(NODES, EDGES),
                payload=None,
                run_agent=env.run_agent,
                run_tool=env.run_tool,
                cancel_event=asyncio.Event(),
                on_await=_broken,
            )
        ]

    events = asyncio.run(_go())
    assert events[-1]["event"] == "error"
    assert events[-1]["code"] == "decision_not_saved"


# --- Reprise -----------------------------------------------------------------


def test_resume_follows_the_chosen_route_without_rerunning_done_nodes():
    env, saved = _Env(), []
    _run(_graph(NODES, EDGES), env, saved=saved)
    calls_before = list(env.calls)
    events = _run(_graph(NODES, EDGES), env, resume=_resume(saved[0], choice="oui"), saved=[])
    assert env.calls == calls_before  # l'agent n'est pas rappelé
    replayed = [e for e in events if e.get("replayed")]
    assert {e["node_id"] for e in replayed} == {"t", "a"}
    complete = next(e for e in events if e["event"] == "node_complete" and e["node_id"] == "d")
    assert complete["output"]["choice"] == "oui"
    assert complete["output"]["answered_by"] == "u@example.com"
    assert {"event": "route", "node_id": "d", "route": "oui"} in events
    assert events[-1] == {"event": "done", "output": "ok:oui"}


def test_resume_with_free_text_takes_the_text_route_in_both_mode():
    nodes = NODES[:2] + [_decision("both")] + NODES[3:]
    edges = EDGES + [{"source": "d", "target": "no", "route": "text"}]
    env, saved = _Env(), []
    _run(_graph(nodes, edges), env, saved=saved)
    events = _run(_graph(nodes, edges), env, resume=_resume(saved[0], text="trop cher"), saved=[])
    assert events[-1] == {"event": "done", "output": "refus:trop cher"}


def test_resume_in_text_mode_passes_the_text_on():
    nodes = NODES[:2] + [_decision("text")] + [
        {"id": "o", "type": "output", "config": {"value": "{{d.text}}"}}
    ]
    edges = EDGES[:2] + [{"source": "d", "target": "o"}]
    env, saved = _Env(), []
    _run(_graph(nodes, edges), env, saved=saved)
    events = _run(_graph(nodes, edges), env, resume=_resume(saved[0], text="vu"), saved=[])
    assert events[-1] == {"event": "done", "output": "vu"}


def test_resume_replays_the_route_a_router_had_taken():
    nodes = [
        {"id": "t", "type": "trigger", "config": {"kind": "manual"}},
        {"id": "lookup", "type": "tool", "config": {"tool": "erp.get", "args": {}}},
        {
            "id": "prio",
            "type": "router",
            "config": {
                "rules": [{"route": "big", "field": "{{lookup.amount}}", "op": "gt", "value": 1000}],
                "default_route": "small",
            },
        },
        {"id": "a", "type": "agent", "config": {"agent_id": "agent1"}},
        {"id": "s", "type": "output", "config": {"value": "small"}},
        _decision("text", question="Q"),
        {"id": "o", "type": "output", "config": {"value": "big:{{d.text}}"}},
    ]
    edges = [
        {"source": "t", "target": "lookup"},
        {"source": "lookup", "target": "prio"},
        {"source": "prio", "target": "a", "route": "big"},
        {"source": "prio", "target": "s", "route": "small"},
        {"source": "a", "target": "d"},
        {"source": "d", "target": "o"},
    ]
    env, saved = _Env(), []
    _run(_graph(nodes, edges), env, saved=saved)
    assert saved[0]["routes"]["prio"] == "big"
    # Le stockage relu n'a plus l'outil : seule la route rejouée mène à « big ».
    snapshot = json.loads(json.dumps(saved[0]))
    snapshot["outputs"]["lookup"] = {"amount": 0}
    events = _run(_graph(nodes, edges), env, resume=_resume(snapshot, text="go"), saved=[])
    assert events[-1] == {"event": "done", "output": "big:go"}
    assert not [e for e in events if e.get("node_id") == "s"]


def test_a_second_decision_pauses_again_with_the_first_answer_kept():
    nodes = NODES[:3] + [
        {"id": "d2", "type": "approval", "config": {"question": "Sûr ? {{d.choice}}", "mode": "text"}},
        {"id": "o", "type": "output", "config": {"value": "{{d.choice}}/{{d2.text}}"}},
    ]
    edges = EDGES[:2] + [
        {"source": "d", "target": "d2", "route": "oui"},
        {"source": "d2", "target": "o"},
    ]
    env, saved = _Env(), []
    _run(_graph(nodes, edges), env, saved=saved)
    second = []
    events = _run(_graph(nodes, edges), env, resume=_resume(saved[0], choice="oui"), saved=second)
    assert events[-1]["event"] == "awaiting_input"
    assert events[-1]["question"] == "Sûr ? oui"
    assert second[0]["outputs"]["d"]["choice"] == "oui"
    answer = {**_answer(text="certain"), "node_id": "d2"}
    events = _run(_graph(nodes, edges), env, resume={**second[0], "answer": answer}, saved=[])
    assert events[-1] == {"event": "done", "output": "oui/certain"}


def test_fan_out_branch_not_yet_run_executes_once_after_resume():
    nodes = NODES[:3] + [
        {"id": "b", "type": "agent", "config": {"agent_id": "agent2"}},
        {"id": "yes", "type": "output", "config": {"value": "ok"}},
        {"id": "no", "type": "output", "config": {"value": "ko"}},
    ]
    edges = EDGES + [{"source": "t", "target": "b"}]
    env, saved = _Env(), []
    _run(_graph(nodes, edges), env, saved=saved)
    _run(_graph(nodes, edges), env, resume=_resume(saved[0], choice="non"), saved=[])
    assert [c[0] for c in env.calls].count("agent2") == 1
    assert [c[0] for c in env.calls].count("agent1") == 1


@pytest.mark.parametrize(
    "mode, answer",
    [
        ("choices", {"choice": "peut-etre"}),
        ("choices", {"text": "libre"}),
        ("text", {"choice": "oui"}),
        ("text", {"text": "  "}),
        ("both", {}),
    ],
)
def test_an_answer_the_decision_does_not_allow_is_refused(mode, answer):
    prompt = {
        "mode": mode,
        "choices": [] if mode == "text" else [{"route": "oui", "label": "Oui"}],
    }
    with pytest.raises(wg.GraphError) as exc:
        wg.check_decision_answer(prompt, answer.get("choice"), answer.get("text"))
    assert exc.value.code == "decision_invalid_answer"


def test_a_decision_reached_inside_a_subworkflow_fails_explicitly():
    child = _graph(
        [{"id": "ct", "type": "trigger", "config": {}}, _decision("text", question="Q")],
        [{"source": "ct", "target": "d"}],
    )

    async def _resolve(_wid):
        return child

    async def _go():
        env = _Env()
        return [
            json.loads(c[6:])
            async for c in wg.run_graph(
                _graph(
                    [NODES[0], {"id": "s", "type": "subworkflow", "config": {"workflow_id": "w2"}}],
                    [{"source": "t", "target": "s"}],
                ),
                payload=None,
                run_agent=env.run_agent,
                run_tool=env.run_tool,
                cancel_event=asyncio.Event(),
                run_subworkflow=_resolve,
                on_await=lambda s: None,
            )
        ]

    events = asyncio.run(_go())
    assert events[-1]["event"] == "error"
    assert "approval_nested" in json.dumps(events[-1])


def test_two_routes_to_the_same_target_run_without_a_decision():
    nodes = [
        {"id": "t", "type": "trigger", "config": {"kind": "manual"}},
        {
            "id": "r",
            "type": "router",
            "config": {
                "rules": [{"route": "a", "field": "{{t.n}}", "op": "gt", "value": 1}],
                "default_route": "b",
            },
        },
        {"id": "o", "type": "output", "config": {"value": "pris:{{t.n}}"}},
    ]
    edges = [
        {"source": "t", "target": "r"},
        {"source": "r", "target": "o", "route": "a"},
        {"source": "r", "target": "o", "route": "b"},
    ]
    for n in (0, 5):
        events = _run(_graph(nodes, edges), _Env(), payload={"n": n})
        assert events[-1] == {"event": "done", "output": f"pris:{n}"}

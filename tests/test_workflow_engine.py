"""Moteur de workflow côté serveur (roadmap#55, étape 1).

Le canvas du builder est exécuté aujourd'hui par le navigateur
(``thaink2/apowerb-ui``, ``src/lib/workflowRunner.js``). Ces tests fixent la
sémantique de ce runner JS, que le moteur serveur doit reproduire à
l'identique, sans appeler de modèle : l'exécution d'un agent feuille est
injectée (``run_leaf``), le reste — ordonnancement, chaînage, fan-out,
jointure, annulation — passe par le vrai ``google.adk.workflow``.
"""

import asyncio
import json

import pytest

from apowerb.core import workflow_engine as we


def _spec(agent_id, agent_type="base", sub_agents=(), description=""):
    return we.AgentSpec(
        agent_id=agent_id,
        agent_type=agent_type,
        sub_agents=list(sub_agents),
        description=description,
    )


class _Catalog:
    """Détails d'agents en mémoire + journal des exécutions feuilles."""

    def __init__(self, *specs, outputs=None, fail=None):
        self.specs = {s.agent_id: s for s in specs}
        self.outputs = outputs or {}
        self.fail = fail or set()
        self.calls = []

    def details_of(self, agent_id):
        return self.specs[agent_id]

    async def run_leaf(self, spec, node_input):
        self.calls.append((spec.agent_id, node_input))
        if spec.agent_id in self.fail:
            raise RuntimeError(f"{spec.agent_id} en panne")
        out = self.outputs.get(spec.agent_id, f"out:{spec.agent_id}")
        return out(node_input) if callable(out) else out


async def _run(catalog, canvas, cancel_event=None):
    events = []
    async for chunk in we.run_canvas(
        canvas,
        details_of=catalog.details_of,
        run_leaf=catalog.run_leaf,
        cancel_event=cancel_event or asyncio.Event(),
    ):
        assert chunk.startswith("data: ") and chunk.endswith("\n\n")
        events.append(json.loads(chunk[len("data: ") :]))
    return events


def _final(events):
    done = [e for e in events if e["event"] == "done"]
    assert len(done) == 1, events
    return done[0]["output"]


# --- Chaînage linéaire -----------------------------------------------------


def test_linear_canvas_chains_outputs_and_first_node_gets_no_input():
    cat = _Catalog(
        _spec("agent1"),
        _spec("agent2"),
        outputs={"agent1": {"total": 2}, "agent2": lambda x: {"seen": x}},
    )
    events = asyncio.run(_run(cat, ["agent1", "agent2"]))
    assert cat.calls == [("agent1", None), ("agent2", {"total": 2})]
    assert _final(events) == {"seen": {"total": 2}}


def test_step_events_follow_execution_order():
    cat = _Catalog(_spec("agent1"), _spec("agent2"))
    events = asyncio.run(_run(cat, ["agent1", "agent2"]))
    steps = [
        (e["event"], e["agent_id"]) for e in events if e["event"].startswith("step_")
    ]
    assert steps == [
        ("step_start", "agent1"),
        ("step_complete", "agent1"),
        ("step_start", "agent2"),
        ("step_complete", "agent2"),
    ]


def test_same_agent_twice_on_canvas_runs_twice():
    cat = _Catalog(_spec("agent1"), outputs={"agent1": lambda x: (x or 0) + 1})
    events = asyncio.run(_run(cat, ["agent1", "agent1", "agent1"]))
    assert _final(events) == 3


def test_router_agent_is_a_single_leaf_like_the_js_runner():
    # Côté JS, un Router s'exécute comme un agent unique : le transfert vers
    # ses sous-agents se fait DANS l'agent (ADK), pas dans le workflow.
    cat = _Catalog(_spec("agent1", "router", ["agent2"]), _spec("agent2"))
    asyncio.run(_run(cat, ["agent1"]))
    assert [c[0] for c in cat.calls] == ["agent1"]


# --- Composites ------------------------------------------------------------


def test_sequential_composite_chains_its_children():
    cat = _Catalog(
        _spec("agent9", "sequential", ["agent1", "agent2"]),
        _spec("agent1"),
        _spec("agent2"),
        _spec("agent3"),
        outputs={
            "agent1": "A",
            "agent2": lambda x: x + "B",
            "agent3": lambda x: x + "C",
        },
    )
    events = asyncio.run(_run(cat, ["agent9", "agent3"]))
    assert cat.calls == [("agent1", None), ("agent2", "A"), ("agent3", "AB")]
    assert _final(events) == "ABC"


def test_parallel_composite_fans_out_and_keeps_sub_agent_order():
    cat = _Catalog(
        _spec("agent0"),
        _spec("agent9", "parallel", ["agent1", "agent2", "agent3"]),
        _spec("agent1"),
        _spec("agent2"),
        _spec("agent3"),
        _spec("agent4"),
        outputs={"agent0": "IN", "agent4": lambda x: {"joined": x}},
    )
    events = asyncio.run(_run(cat, ["agent0", "agent9", "agent4"]))
    fanned = sorted(c for c in cat.calls if c[0] in {"agent1", "agent2", "agent3"})
    assert fanned == [("agent1", "IN"), ("agent2", "IN"), ("agent3", "IN")]
    # Le runner JS renvoie un tableau dans l'ordre des sous-agents
    # (Promise.all), quel que soit l'ordre d'arrivée.
    assert _final(events) == {"joined": ["out:agent1", "out:agent2", "out:agent3"]}


def test_composite_without_children_runs_as_a_leaf():
    # JS : ``agent.category === "Sequential" && agent.subAgents?.length > 0``,
    # sinon l'agent tombe dans la branche « base ».
    cat = _Catalog(_spec("agent9", "sequential", []))
    asyncio.run(_run(cat, ["agent9"]))
    assert cat.calls == [("agent9", None)]


def test_loop_is_refused_before_any_agent_runs():
    # La sémantique de boucle qui fait foi n'est pas tranchée (roadmap#55,
    # décision D1) : on refuse plutôt que d'en choisir une en silence.
    cat = _Catalog(_spec("agent1"), _spec("agent9", "loop", ["agent1"]))
    events = asyncio.run(_run(cat, ["agent1", "agent9"]))
    assert cat.calls == []
    assert events[-1]["event"] == "error"
    assert "loop" in events[-1]["detail"].lower()


# --- Erreurs et annulation --------------------------------------------------


def test_failing_agent_stops_the_run_and_reports_it():
    cat = _Catalog(_spec("agent1"), _spec("agent2"), _spec("agent3"), fail={"agent2"})
    events = asyncio.run(_run(cat, ["agent1", "agent2", "agent3"]))
    assert [c[0] for c in cat.calls] == ["agent1", "agent2"]
    assert {"event": "step_error", "agent_id": "agent2"}.items() <= next(
        e for e in events if e["event"] == "step_error"
    ).items()
    assert events[-1]["event"] == "error"
    assert not any(e["event"] == "done" for e in events)


def test_cancel_stops_before_the_next_agent():
    cancel = asyncio.Event()
    cat = _Catalog(_spec("agent1"), _spec("agent2"))

    async def _leaf(spec, node_input):
        cat.calls.append((spec.agent_id, node_input))
        cancel.set()
        return "x"

    async def _go():
        out = []
        async for chunk in we.run_canvas(
            ["agent1", "agent2"],
            details_of=cat.details_of,
            run_leaf=_leaf,
            cancel_event=cancel,
        ):
            out.append(json.loads(chunk[len("data: ") :]))
        return out

    events = asyncio.run(_go())
    assert [c[0] for c in cat.calls] == ["agent1"]
    assert events[-1]["event"] == "cancelled"


def test_empty_canvas_is_an_error_not_a_silent_success():
    events = asyncio.run(_run(_Catalog(), []))
    assert events[-1]["event"] == "error"


# --- Transport entre agents (portage de workflowRunner.js) -----------------


@pytest.mark.parametrize(
    "node_input, description, expected",
    [
        (None, "", "Execute your task now and perform the required action."),
        (None, "Résume", "Execute your task: Résume"),
        ({"a": 1}, "ignored", json.dumps({"a": 1})),
        ("texte", "", json.dumps("texte")),
    ],
)
def test_leaf_message_matches_the_js_runner(node_input, description, expected):
    assert we.leaf_message(node_input, description) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ('Voici : {"a": [1, 2]}', {"a": [1, 2]}),
        ("pas du json", "pas du json"),
        ("[1, 2]", [1, 2]),
    ],
)
def test_try_parse_json_matches_the_js_runner(text, expected):
    assert we.try_parse_json(text) == expected


def test_extract_response_text_takes_the_last_text_event():
    events = [
        {"content": {"parts": [{"text": "intermédiaire"}]}},
        {"content": {"parts": [{"function_call": {"name": "t"}}]}},
        {"content": {"parts": [{"text": "final"}]}},
        {"content": {"parts": [{"function_response": {}}]}},
    ]
    assert we.extract_response_text(events) == "final"


def test_extract_response_text_skips_the_reasoning_summary():
    # Gemini 2.5 : le résumé de raisonnement précède la réponse dans le même
    # événement, dans une part marquée ``thought``.
    events = [
        {
            "content": {
                "parts": [
                    {"text": "Here is my thought process...", "thought": True},
                    {"text": "Paris est la capitale de la France."},
                ]
            }
        }
    ]
    assert we.extract_response_text(events) == "Paris est la capitale de la France."


def test_extract_response_text_ignores_a_reasoning_only_event():
    events = [
        {"content": {"parts": [{"text": "Paris."}]}},
        {"content": {"parts": [{"text": "je réfléchis", "thought": True}]}},
    ]
    assert we.extract_response_text(events) == "Paris."


def test_extract_response_text_joins_the_answer_parts():
    events = [{"content": {"parts": [{"text": "Pa"}, {"text": "ris."}]}}]
    assert we.extract_response_text(events) == "Paris."


# --- Branchement production ------------------------------------------------


def test_router_keeps_the_stub_unless_the_server_engine_is_requested(monkeypatch):
    # Le front exécute encore le canvas lui-même : activer le moteur serveur
    # par défaut ferait tourner chaque agent deux fois.
    from apowerb.routers import workflows

    monkeypatch.delenv("APOWERB_WORKFLOW_ENGINE", raising=False)
    assert workflows._select_runner() is workflows._default_workflow_runner
    monkeypatch.setenv("APOWERB_WORKFLOW_ENGINE", "server")
    assert workflows._select_runner() is workflows._server_workflow_runner


def test_owner_scoped_specs_normalise_ids_and_read_sub_agents(monkeypatch):
    import apowerb.core.agent_helpers as helpers

    rows = {
        7: {
            "owner_id": "a@x.fr",
            "agent_type": "Parallel",
            "sub_agents": "['agent8', 'agent9']",
            "agent_description": "d",
        }
    }
    monkeypatch.setattr(
        helpers, "get_agent_details", lambda agent_id, **_: rows.get(agent_id, {})
    )
    details_of = we.owner_scoped_specs("a@x.fr")
    spec = details_of("7")
    assert (spec.agent_id, spec.sub_agents, spec.description) == (
        "agent7",
        ["agent8", "agent9"],
        "d",
    )
    assert we._kind(spec) == "parallel"
    with pytest.raises(we.UnsupportedWorkflow):
        details_of("agent404")
    with pytest.raises(we.UnsupportedWorkflow):
        details_of("../etc")


def test_http_leaf_runner_goes_through_guards_session_and_run(monkeypatch):
    import apowerb.core.adk_runner as adk
    import apowerb.core.run_gate as gate

    calls = []

    async def _guards(**kw):
        calls.append(("guards", kw["agent_name"], kw["owner_id"], kw["plan"]))

    async def _session(**kw):
        calls.append(("session", kw["agent_name"], kw["user_id"], kw["token"]))

    async def _run(**kw):
        calls.append(("run", kw["new_message"]["parts"][0]["text"], kw["session_id"]))
        return [{"content": {"parts": [{"text": '```json\n{"ok": true}\n```'}]}}]

    monkeypatch.setattr(gate, "apply_run_guards", _guards)
    monkeypatch.setattr(adk, "create_adk_agent_session", _session)
    monkeypatch.setattr(adk, "run_adk_agent", _run)

    leaf = we.make_http_leaf_runner(
        owner_email="a@x.fr", plan="pro", token_factory=lambda: "T"
    )
    out = asyncio.run(leaf(_spec("agent3"), {"q": 1}))
    assert out == {"ok": True}
    assert calls[0] == ("guards", "agent3", "a@x.fr", "pro")
    assert calls[1] == ("session", "agent3", "a@x.fr", "T")
    assert calls[2][1] == '{"q": 1}'
    assert calls[2][2].startswith("workflow_agent3_")


def test_quota_refusal_stops_the_workflow_before_the_agent_runs(monkeypatch):
    import apowerb.core.adk_runner as adk
    import apowerb.core.run_gate as gate
    from fastapi import HTTPException

    ran = []

    async def _refuse(**kw):
        raise HTTPException(status_code=402, detail="quota")

    async def _run(**kw):
        ran.append(kw)

    monkeypatch.setattr(gate, "apply_run_guards", _refuse)
    monkeypatch.setattr(adk, "run_adk_agent", _run)
    cat = _Catalog(_spec("agent1"))
    leaf = we.make_http_leaf_runner(
        owner_email="a@x.fr", plan=None, token_factory=lambda: "T"
    )

    async def _go():
        return [
            json.loads(c[6:])
            async for c in we.run_canvas(
                ["agent1"],
                details_of=cat.details_of,
                run_leaf=leaf,
                cancel_event=asyncio.Event(),
            )
        ]

    events = asyncio.run(_go())
    assert ran == []
    assert events[-1]["event"] == "error"


# --- Constats de revue (21/09) ---------------------------------------------


def test_canvas_refuses_an_agent_of_another_owner(monkeypatch):
    # Revue n°1 : ``get_agent_details`` ne filtre pas par propriétaire ; sans
    # contrôle, le moteur canvas exécutait l'agent d'un autre client.
    import apowerb.core.agent_helpers as helpers

    rows = {
        5: {
            "owner_id": "bob@other.fr",
            "agent_type": "loop",
            "sub_agents": "['agent6']",
        }
    }
    monkeypatch.setattr(
        helpers, "get_agent_details", lambda agent_id, **_: rows.get(agent_id, {})
    )
    details_of = we.owner_scoped_specs("alice@acme.fr")
    with pytest.raises(we.UnsupportedWorkflow, match="introuvable") as err:
        details_of("agent5")
    assert "loop" not in str(err.value)  # rien de l'agent d'autrui ne fuit
    assert we.owner_scoped_specs("bob@other.fr")("agent5").agent_type == "loop"


def test_unexpected_errors_are_not_echoed_to_the_client():
    # Revue n°3 : une exception d'outil ou de bibliothèque peut porter une URL
    # avec clé ; seules nos propres erreurs sont affichables telles quelles.
    cat = _Catalog(_spec("agent1"))

    async def _leaky(spec, node_input):
        raise RuntimeError("GET https://api.x/v1?key=SECRET123 failed")

    async def _go():
        return [
            json.loads(c[6:])
            async for c in we.run_canvas(
                ["agent1"],
                details_of=cat.details_of,
                run_leaf=_leaky,
                cancel_event=asyncio.Event(),
            )
        ]

    events = asyncio.run(_go())
    assert "SECRET123" not in json.dumps(events)
    assert events[-1]["event"] == "error"
    assert any(e["event"] == "step_error" and e["agent_id"] == "agent1" for e in events)


def test_each_agent_call_gets_a_fresh_token(monkeypatch):
    # Revue n°4 : un seul jeton de 30 min pour tout le run faisait échouer les
    # derniers nœuds d'un run long.
    import apowerb.core.adk_runner as adk
    import apowerb.core.run_gate as gate

    tokens = []

    async def _noop(**kw):
        return None

    async def _run(**kw):
        tokens.append(kw["token"])
        return "ok"

    monkeypatch.setattr(gate, "apply_run_guards", _noop)
    monkeypatch.setattr(adk, "create_adk_agent_session", _noop)
    monkeypatch.setattr(adk, "run_adk_agent", _run)
    counter = iter(range(100))
    leaf = we.make_http_leaf_runner(
        owner_email="a@x.fr", plan=None, token_factory=lambda: f"T{next(counter)}"
    )
    asyncio.run(leaf(_spec("agent1"), None))
    asyncio.run(leaf(_spec("agent2"), None))
    assert tokens == ["T0", "T1"]

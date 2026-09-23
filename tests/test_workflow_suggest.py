"""POST /api/workflows/defs/suggest-next : le nœud suivant proposé par un modèle.

Aucun appel réseau ni base : le modèle (``litellm.acompletion``), le portier
(``apply_run_guards``), la liste d'agents et l'écriture ``llm_usage`` sont
remplacés. Ce qu'on vérifie, c'est le contrat : ce qui part au modèle, ce
qui revient à l'éditeur, et ce qui est consigné.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import litellm
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from apowerb.configs.settings import get_settings
from apowerb.core import run_gate, workflow_suggest
from apowerb.core.agent_helpers import usage_recorder

ALICE = "alice@acme.fr"
SECRET_SAMPLE = {"email": "client@secret.fr", "subject": "Devis urgent pour Dupont"}
GRAPH = {
    "version": 1,
    "nodes": [
        {
            "id": "start",
            "type": "trigger",
            "label": "Mail entrant",
            "config": {"kind": "manual", "sample_payload": SECRET_SAMPLE},
        },
        {
            "id": "notif",
            "type": "notification",
            "config": {"channel": "email", "to": ["boss@corp.fr"], "subject": "x", "body": "{{start}}"},
        },
        {
            "id": "call",
            "type": "http",
            "config": {"method": "GET", "url": "https://api.corp.fr/items?token=abc123"},
        },
    ],
    "edges": [],
}
CLASSIFIER = {
    "type": "classifier",
    "label": "Tri des demandes",
    "config": {
        "agent_id": "agent3",
        "input": "{{start.subject}}",
        "routes": [
            {"route": "devis", "description": "demande de devis"},
            {"route": "reclamation", "description": "plainte"},
        ],
    },
    "reason": "Les mails mêlent devis et réclamations.",
}
OUTPUT = {"type": "output", "config": {"value": "{{start}}"}, "reason": "Rendre le résultat."}


def _response(payload, usage=(100, 20, 120)):
    content = payload if isinstance(payload, str) else json.dumps(payload)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(
            prompt_tokens=usage[0], completion_tokens=usage[1], total_tokens=usage[2]
        ),
    )


@pytest.fixture()
def env(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "workflow_suggest_enabled", True)
    monkeypatch.setattr(settings, "workflow_suggest_model", "")
    monkeypatch.setattr(settings, "default_llm_model", "gemini/gemini-test")
    monkeypatch.setattr(settings, "default_llm_api_key", "k-test")
    monkeypatch.setattr(settings, "default_llm_api_base", "")

    state = SimpleNamespace(calls=[], guards=[], usage=[], reply=_response({"suggestions": [CLASSIFIER, OUTPUT]}))

    async def fake_completion(**kwargs):
        state.calls.append(kwargs)
        if isinstance(state.reply, Exception):
            raise state.reply
        return state.reply

    async def fake_guards(**kwargs):
        state.guards.append(kwargs)

    async def fake_plan(owner_id):
        return None

    async def fake_persist(**fields):
        state.usage.append(fields)

    monkeypatch.setattr(litellm, "acompletion", fake_completion)
    monkeypatch.setattr(run_gate, "apply_run_guards", fake_guards)
    monkeypatch.setattr(run_gate, "resolve_owner_plan", fake_plan)
    monkeypatch.setattr(usage_recorder, "_persist_usage_row", fake_persist)
    monkeypatch.setattr(
        workflow_suggest,
        "_owner_agents",
        lambda owner: [{"agent_id": "agent3", "name": "Tri des mails", "description": "trie les mails"}],
    )

    from apowerb.auth.dependencies import get_current_user
    from apowerb.routers import workflow_defs

    user = MagicMock()
    user.email, user.user_id, user.role = ALICE, 1, "USER"
    app = FastAPI()
    app.include_router(workflow_defs.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: user
    state.client = TestClient(app)
    state.settings = settings
    return state


def _suggest(env, node_id="start", graph=GRAPH, **extra):
    return env.client.post(
        "/api/workflows/defs/suggest-next",
        json={"graph": graph, "node_id": node_id, "name": "Tri des mails", **extra},
    )


def test_off_by_default_and_the_model_is_never_called(env, monkeypatch):
    monkeypatch.setattr(env.settings, "workflow_suggest_enabled", False)
    r = _suggest(env)
    assert r.status_code == 404
    assert r.json()["detail"] == {"code": "SUGGEST_DISABLED"}
    assert env.calls == [] and env.guards == []


def test_off_without_the_shared_model_even_when_switched_on(env, monkeypatch):
    monkeypatch.setattr(env.settings, "default_llm_api_key", "")
    assert _suggest(env).status_code == 404
    assert env.calls == []


def test_valid_proposals_come_back_complete_and_the_call_is_capped_and_counted(env):
    r = _suggest(env)
    assert r.status_code == 200
    body = r.json()
    assert body["route"] is None
    assert [s["type"] for s in body["suggestions"]] == ["classifier", "output"]
    assert body["suggestions"][0]["config"] == CLASSIFIER["config"]
    assert body["suggestions"][0]["reason"] == CLASSIFIER["reason"]

    assert env.guards == [{"agent_name": "workflow_suggest", "owner_id": ALICE, "plan": None}]
    assert env.usage == [
        {
            "agent_id": 0,
            "agent_name": "workflow_suggest",
            "owner_id": ALICE,
            "invocation_source": "workflow_suggest",
            "model": "gemini/gemini-test",
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "billed_to_thaink2": True,
        }
    ]
    call = env.calls[0]
    assert call["timeout"] == 4.0 and call["num_retries"] == 0
    assert call["response_format"] == {"type": "json_object"}


def test_the_model_sees_the_structure_never_the_values(env):
    _suggest(env)
    system, user = env.calls[0]["messages"]
    # Du graphe, le prompt système ne reçoit que l'identifiant sélectionné.
    assert system["content"] == workflow_suggest.system_prompt("start")
    sent = user["content"]
    view = json.loads(sent)

    assert view["nodes"][0]["payload_fields"] == ["email", "subject"]
    assert view["agents"] == [{"agent_id": "agent3", "name": "Tri des mails", "description": "trie les mails"}]
    for leaked in ("client@secret.fr", "Dupont", "boss@corp.fr", "token=abc123", "api.corp.fr", "{{start}}"):
        assert leaked not in sent, leaked


def test_a_dedicated_model_replaces_the_shared_one(env, monkeypatch):
    monkeypatch.setattr(env.settings, "workflow_suggest_model", "gemini/gemini-lite")
    _suggest(env)
    assert env.calls[0]["model"] == "gemini/gemini-lite"
    assert env.usage[0]["model"] == "gemini/gemini-lite"


@pytest.mark.parametrize(
    "bad",
    [
        {"type": "tool", "config": {"tool": "x.lookup"}},
        {"type": "trigger", "config": {"kind": "manual"}},
        {"type": "agent", "config": {"agent_id": "agent9", "input": "{{start}}"}},
        {"type": "classifier", "config": {"agent_id": "agent3", "routes": [{"route": "seul"}]}},
        {"type": "convert", "config": {"to": "xml", "input": "{{start}}"}},
        {"type": "agent", "config": {"agent_id": "agent3", "input": "{{call}}"}},
        {"type": "agent", "config": {"agent_id": "agent3", "input": "{{ailleurs.x}}"}},
        {"type": "set", "config": {"fields": [{"key": "a"}] * 2}},
        "pas un objet",
    ],
    ids=[
        "tool", "second-trigger", "agent-not-owned", "one-route-classifier",
        "unknown-conversion", "ref-not-upstream", "ref-unknown", "duplicate-set-keys", "not-an-object",
    ],
)
def test_a_proposal_that_breaks_a_rule_is_dropped_not_repaired(env, bad):
    env.reply = _response({"suggestions": [bad, OUTPUT]})
    r = _suggest(env)
    assert r.status_code == 200
    assert [s["config"] for s in r.json()["suggestions"]] == [OUTPUT["config"]]


def test_at_most_two_and_one_per_type(env):
    other_output = {"type": "output", "config": {"value": "{{start.email}}"}}
    agent = {"type": "agent", "config": {"agent_id": "agent3", "input": "{{start}}"}}
    env.reply = _response({"suggestions": [OUTPUT, other_output, agent, CLASSIFIER]})
    assert [s["type"] for s in _suggest(env).json()["suggestions"]] == ["output", "agent"]


def test_the_branch_to_wire_is_computed_here_not_asked_to_the_model(env):
    graph = {
        "version": 1,
        "nodes": [
            {"id": "start", "type": "trigger", "config": {"kind": "manual"}},
            {"id": "r", "type": "router", "config": {"rules": [{"route": "urgent"}], "default_route": "normal"}},
            {"id": "o", "type": "output", "config": {}},
        ],
        "edges": [
            {"source": "start", "target": "r"},
            {"source": "r", "target": "o", "route": "urgent"},
        ],
    }
    env.reply = _response({"suggestions": [{"type": "output", "config": {"value": "{{r}}"}}]})
    body = _suggest(env, node_id="r", graph=graph).json()
    assert body["route"] == "normal"
    assert json.loads(env.calls[0]["messages"][1]["content"])["branch_to_fill"] == "normal"
    assert [s["type"] for s in body["suggestions"]] == ["output"]


def test_nothing_to_fill_means_no_call_at_all(env):
    graph = {
        "version": 1,
        "nodes": [
            {"id": "start", "type": "trigger", "config": {"kind": "manual"}},
            {"id": "o", "type": "output", "config": {}},
        ],
        "edges": [{"source": "start", "target": "o"}],
    }
    for node in ("start", "o"):
        assert _suggest(env, node_id=node, graph=graph).json() == {"route": None, "suggestions": []}
    assert env.calls == [] and env.guards == [] and env.usage == []


def test_cap_reached_refuses_before_the_model_is_called(env, monkeypatch):
    async def refuse(**kwargs):
        raise HTTPException(402, {"code": "TOKEN_QUOTA_EXCEEDED"})

    monkeypatch.setattr(run_gate, "apply_run_guards", refuse)
    r = _suggest(env)
    assert r.status_code == 402
    assert r.json()["detail"]["code"] == "TOKEN_QUOTA_EXCEEDED"
    assert env.calls == [] and env.usage == []


def test_a_slow_model_is_a_clear_503_not_an_empty_list(env):
    env.reply = litellm.Timeout("trop lent", model="gemini/gemini-test", llm_provider="gemini")
    r = _suggest(env)
    assert r.status_code == 503
    assert r.json()["detail"] == {"code": "SUGGEST_UNAVAILABLE", "reason": "timeout"}
    assert env.usage == []


def test_unusable_output_is_a_503_but_its_tokens_are_still_counted(env):
    env.reply = _response("désolé, je ne peux pas")
    r = _suggest(env)
    assert r.status_code == 503
    assert r.json()["detail"] == {"code": "SUGGEST_UNAVAILABLE", "reason": "bad_output"}
    assert [u["total_tokens"] for u in env.usage] == [120]


def test_unknown_node_and_malformed_graph_are_422(env):
    assert _suggest(env, node_id="nope").status_code == 422
    assert _suggest(env, graph={"version": 1, "nodes": [{"id": "x", "type": "nope"}]}).status_code == 422
    assert env.calls == []


def test_public_config_announces_the_feature_only_when_it_is_served(env, monkeypatch):
    from apowerb.routers import config

    app = FastAPI()
    app.include_router(config.router, prefix="/api")
    client = TestClient(app)
    assert client.get("/api/config").json()["workflow_suggest_enabled"] is True
    monkeypatch.setattr(env.settings, "workflow_suggest_enabled", False)
    assert client.get("/api/config").json()["workflow_suggest_enabled"] is False


# --- Forme de la configuration enseignée au modèle (roadmap#89) --------------


@pytest.mark.parametrize("node_type", workflow_suggest.SUGGESTIBLE_TYPES)
def test_every_proposable_type_has_a_shape_whose_example_the_validator_accepts(node_type):
    from apowerb.core.workflow_graph import Node, validate_node

    rule, example = workflow_suggest.CONFIG_SHAPES[node_type]
    validate_node(Node(id="x", type=node_type, config=example))
    assert rule and json.dumps(example) in workflow_suggest._SYSTEM


def test_the_table_is_the_single_list_of_proposable_types():
    assert tuple(workflow_suggest.CONFIG_SHAPES) == workflow_suggest.SUGGESTIBLE_TYPES


def test_examples_only_read_the_selected_node():
    from apowerb.core.workflow_graph import Node, _outer_refs

    for node_type, (_, example) in workflow_suggest.CONFIG_SHAPES.items():
        refs = _outer_refs(Node(id="x", type=node_type, config=example))
        assert refs <= {workflow_suggest.SELECTED}, node_type


def test_the_rule_operators_taught_are_exactly_those_the_engine_runs():
    from apowerb.core.workflow_graph import GraphError, evaluate_rule

    for op in workflow_suggest.RULE_OPS:
        evaluate_rule({"op": op, "value": [1]}, 1)
        assert op in workflow_suggest._SYSTEM
    with pytest.raises(GraphError):
        evaluate_rule({"op": "between", "value": 1}, 1)


def test_the_prompt_teaches_the_choices_the_validator_enforces():
    from apowerb.core.workflow_graph import CONVERT_TARGETS, EXTRACT_FIELD_TYPES

    for choice in (*CONVERT_TARGETS, *EXTRACT_FIELD_TYPES):
        assert choice in workflow_suggest._SYSTEM
    # Le modèle ne voit jamais d'adresse (describe_graph les tait) : il ne
    # peut proposer qu'une notification dans l'application.
    rule, example = workflow_suggest.CONFIG_SHAPES["notification"]
    assert example["channel"] == "app" and "to" not in example


def test_the_examples_carry_the_real_selected_id_not_a_placeholder(env):
    """Banc #89 : avec ``{{SELECTED}}`` dans les exemples, le modèle le
    recopiait tel quel et 39 % des propositions tombaient (hors amont)."""
    _suggest(env)
    system = env.calls[0]["messages"][0]["content"]
    assert workflow_suggest.SELECTED not in system
    assert '"input": "{{start}}"' in system and "{{start.field}}" in system

"""The agents API reads back what it accepts, and 404s on a missing agent.

``GET /api/agents`` hands out ``agent_model_params``, ``input_schema``,
``output_schema`` and ``guardrails_config`` as the JSON strings stored in the
database (``"{}"``, ``"null"``); ``GET /api/agents/{id}`` does the same for the
two schemas. The PUT expected objects, so sending an agent back unchanged was
refused with a 422. The PUT now takes either form: the GET output is left as
is, since existing clients parse those strings.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apowerb.auth.dependencies import get_current_user
from apowerb.core import agent_main
from apowerb.core.agent_helpers.default_llm import MASKED_API_KEY
from apowerb.routers import agents as router

OWNER = "u@example.com"


@pytest.fixture
def client(monkeypatch):
    received = {}

    def _update(agent_id, agent, user_id):
        received["agent"] = agent
        return {"agent_id": f"agent{agent_id}"}

    monkeypatch.setattr(router, "update_agent_func", _update)
    monkeypatch.setattr(router, "validate_agent_model", lambda *a, **k: None)
    app = FastAPI()
    app.include_router(router.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(email=OWNER)
    test_client = TestClient(app)
    test_client.received = received
    return test_client


def _as_read_back(**overrides):
    """An agent as GET /api/agents returns it: JSON columns still strings."""
    agent = {
        "agent_id": 7,
        "agent_name": "support",
        "agent_model": "gpt-4o-mini",
        "agent_model_params": json.dumps({"temperature": 0.2}),
        "agent_description": "d",
        "agent_instruction": "i",
        "agent_tools": [],
        "agent_type": "llm_agent",
        "input_schema": "null",
        "output_schema": json.dumps({"type": "object"}),
        "guardrails_config": json.dumps({"pii": True}),
        "memory_enabled": "false",
        "integrity_errors": [],
    }
    agent.update(overrides)
    return agent


def test_an_agent_read_back_is_accepted_unchanged_by_the_put(client):
    response = client.put("/api/agents/agent7", json=_as_read_back())

    assert response.status_code == 200, response.text
    agent = client.received["agent"]
    assert agent.agent_model_params == {"temperature": 0.2}
    assert agent.input_schema is None
    assert agent.output_schema == {"type": "object"}
    assert agent.guardrails_config == {"pii": True}


def test_objects_are_still_accepted(client):
    payload = _as_read_back(
        agent_model_params={"temperature": 0.2},
        input_schema=None,
        output_schema={"type": "object"},
        guardrails_config={"pii": True},
    )

    response = client.put("/api/agents/agent7", json=payload)

    assert response.status_code == 200, response.text
    assert client.received["agent"].output_schema == {"type": "object"}


def test_a_string_that_is_not_a_json_object_is_still_refused(client):
    response = client.put("/api/agents/agent7", json=_as_read_back(output_schema="not json"))

    assert response.status_code == 422


def test_a_missing_agent_is_a_404(client, monkeypatch):
    monkeypatch.setattr(router, "get_agent", lambda agent_id, user_id: {})

    response = client.get("/api/agents/agent999999")

    assert response.status_code == 404


def test_the_list_never_hands_out_the_stored_key(monkeypatch, tmp_path):
    """Sent back through the PUT, the stored ciphertext would be encrypted again.

    The list gives the same mask as ``GET /api/agents/{id}``, which the PUT
    already swaps back for the stored key. The field stays a JSON string.
    """
    row = SimpleNamespace(
        _asdict=lambda: {
            "agent_id": 7,
            "agent_model_params": json.dumps(
                {"model_api_key": "gAAAA-ciphertext", "temperature": 0.2}
            ),
        }
    )
    monkeypatch.setattr(
        type(agent_main.agent_store), "get_list_agents", lambda self, query: [row]
    )
    monkeypatch.setattr(agent_main, "agents_pool_dir", lambda: tmp_path)

    [agent] = agent_main.fetch_agents(user_id=OWNER)

    assert isinstance(agent["agent_model_params"], str)
    assert json.loads(agent["agent_model_params"]) == {
        "model_api_key": MASKED_API_KEY,
        "temperature": 0.2,
    }

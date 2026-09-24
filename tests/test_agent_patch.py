"""``PATCH /api/agents/{id}`` changes only the fields it is sent.

The PUT replaces the whole agent, so ``{"agent_model": ...}`` alone was refused
for missing fields: changing one setting meant reading the agent, editing it and
sending all of it back. The PATCH does that merge on the server, from the stored
agent as ``GET /api/agents/{id}`` returns it, then goes through the same checks
and the same write as the PUT.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apowerb.auth.dependencies import get_current_user
from apowerb.routers import agents as router

OWNER = "u@example.com"

STORED = {
    "agent_id": 7,
    "agent_name": "support",
    "agent_model": "gpt-4o-mini",
    "agent_model_params": {"temperature": 0.2, "model_api_key": "__unchanged__"},
    "agent_description": "d",
    "agent_instruction": "i",
    "agent_tools": ["tool_a"],
    "agent_type": "llm_agent",
    "input_schema": "null",
    "output_schema": json.dumps({"type": "object"}),
    "memory_enabled": True,
    "integrity_errors": [],
}


@pytest.fixture
def client(monkeypatch):
    received = {}

    def _update(agent_id, agent, user_id):
        received["id"] = agent_id
        received["agent"] = agent
        return {"agent_id": f"agent{agent_id}"}

    monkeypatch.setattr(router, "update_agent_func", _update)
    monkeypatch.setattr(router, "validate_agent_model", lambda *a, **k: None)
    monkeypatch.setattr(
        router, "get_agent", lambda agent_id, user_id: dict(STORED) if agent_id == 7 else {}
    )
    app = FastAPI()
    app.include_router(router.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(email=OWNER)
    test_client = TestClient(app)
    test_client.received = received
    return test_client


def test_a_patch_changes_only_the_fields_it_sends(client):
    response = client.patch("/api/agents/agent7", json={"agent_model": "gpt-4o"})

    assert response.status_code == 200, response.text
    agent = client.received["agent"]
    assert client.received["id"] == 7
    assert agent.agent_model == "gpt-4o"
    assert agent.agent_instruction == "i"
    assert agent.agent_tools == ["tool_a"]
    assert agent.memory_enabled is True
    assert agent.output_schema == {"type": "object"}
    # The masked key goes back as the mask, which the write swaps for the stored key.
    assert agent.agent_model_params == {"temperature": 0.2, "model_api_key": "__unchanged__"}
    assert agent.owner_id == OWNER


def test_a_patch_on_a_missing_agent_is_a_404(client):
    response = client.patch("/api/agents/agent999", json={"agent_model": "gpt-4o"})

    assert response.status_code == 404
    assert "agent" not in client.received


def test_an_unknown_field_is_refused(client):
    response = client.patch("/api/agents/agent7", json={"agent_modle": "gpt-4o"})

    assert response.status_code == 422
    assert "agent_modle" in response.text
    assert "agent" not in client.received


def test_an_invalid_value_is_refused(client):
    response = client.patch("/api/agents/agent7", json={"output_schema": "not json"})

    assert response.status_code == 422
    assert "agent" not in client.received


def test_a_model_the_guard_refuses_is_a_422(client, monkeypatch):
    def _refuse(*args, **kwargs):
        raise ValueError("unknown provider")

    monkeypatch.setattr(router, "validate_agent_model", _refuse)

    response = client.patch("/api/agents/agent7", json={"agent_model": "nope/x"})

    assert response.status_code == 422
    assert "unknown provider" in response.text
    assert "agent" not in client.received

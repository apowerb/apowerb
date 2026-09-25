"""A tool the agent lacks can be added from the chat, on the user's approval.

An agent only runs the tools of its config and of its template: connecting
Gmail gave an agent no way to read mail. The agent now looks the tool up
(``find_tools``), offers it (``propose_agent_upgrade``, which refuses names
that are not addable catalogue tools) and, once the user approves,
``POST /api/agents/{id}/tools`` adds it through the PATCH path:

  - an integration tool is added and the agent reloaded,
  - adding it twice changes nothing,
  - an unknown tool, or one that needs a Tool Config, is refused (422)
    without any write,
  - an agent the caller does not own is a 404, without any write.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apowerb.auth.dependencies import get_current_user
from apowerb.core.agent_helpers.chat_action_tools import propose_agent_upgrade
from apowerb.core.agent_helpers.tool_catalog import find_tools
from apowerb.routers import agents as router

OWNER = "u@example.com"
GMAIL = "google_gmail.tool_list_emails"
NEEDS_CONFIG = "database.tool_run_sql"

STORED = {
    "agent_id": 7,
    "agent_name": "jev",
    "agent_model": "gpt-4o-mini",
    "agent_model_params": {"temperature": 0.1},
    "agent_description": "d",
    "agent_instruction": "i",
    "agent_tools": ["jev.tool_jev_classify"],
    "agent_type": "llm_agent",
    "input_schema": "null",
    "output_schema": json.dumps({"type": "object"}),
    "integrity_errors": [],
}


@pytest.fixture
def client(monkeypatch):
    calls = {"writes": [], "reloads": []}

    def _update(agent_id, agent, user_id):
        calls["writes"].append((agent_id, list(agent.agent_tools), user_id))
        return {"agent_id": f"agent{agent_id}"}

    monkeypatch.setattr(router, "update_agent_func", _update)
    monkeypatch.setattr(router, "validate_agent_model", lambda *a, **k: None)
    monkeypatch.setattr(
        router,
        "invalidate_agent_runtime",
        lambda state, agent_id: calls["reloads"].append(agent_id),
    )
    monkeypatch.setattr(
        router,
        "get_agent",
        lambda agent_id, user_id: dict(STORED)
        if agent_id == 7 and user_id == OWNER
        else {},
    )
    app = FastAPI()
    app.include_router(router.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(email=OWNER)
    test_client = TestClient(app)
    test_client.calls = calls
    return test_client


def test_an_integration_tool_is_added_and_the_agent_reloaded(client):
    response = client.post("/api/agents/agent7/tools", json={"tool_name": GMAIL})

    assert response.status_code == 200, response.text
    assert response.json() == {
        "added": True,
        "agent_tools": ["jev.tool_jev_classify", GMAIL],
    }
    assert client.calls["writes"] == [(7, ["jev.tool_jev_classify", GMAIL], OWNER)]
    assert client.calls["reloads"] == ["7"]


def test_adding_a_tool_the_agent_has_changes_nothing(client):
    response = client.post(
        "/api/agents/agent7/tools", json={"tool_name": "jev.tool_jev_classify"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["added"] is False
    assert client.calls["writes"] == []


@pytest.mark.parametrize("tool_name", ["nope.tool_x", NEEDS_CONFIG, "", "overlay.tool_x"])
def test_a_tool_that_cannot_be_added_is_refused_without_a_write(client, tool_name):
    response = client.post("/api/agents/agent7/tools", json={"tool_name": tool_name})

    assert response.status_code == 422
    assert client.calls["writes"] == []


def test_a_tool_needing_a_tool_config_names_the_tool_box(client):
    response = client.post("/api/agents/agent7/tools", json={"tool_name": NEEDS_CONFIG})

    assert "Tool Box" in response.json()["detail"]


def test_an_agent_the_caller_does_not_own_is_a_404(client):
    response = client.post("/api/agents/agent8/tools", json={"tool_name": GMAIL})

    assert response.status_code == 404
    assert client.calls["writes"] == []


def test_find_tools_returns_the_gmail_reader_as_addable():
    result = find_tools("gmail list emails")

    assert result["status"] == "success"
    by_name = {t["tool_name"]: t for t in result["tools"]}
    assert GMAIL in by_name
    assert by_name[GMAIL]["needs_integration"] is True
    assert by_name[GMAIL]["addable"] is True


def test_find_tools_flags_a_tool_needing_a_tool_config():
    by_name = {t["tool_name"]: t for t in find_tools("database run sql")["tools"]}

    assert by_name[NEEDS_CONFIG]["addable"] is False


def test_find_tools_without_usable_keywords_finds_nothing():
    assert find_tools("a")["status"] == "not_found"


def test_the_card_refuses_a_tool_name_it_cannot_add():
    result = propose_agent_upgrade(capability="OCR", reason="r", tool_name="pdf_ocr")

    assert result["status"] == "error"
    assert "find_tools" in result["message"]


def test_the_card_is_shown_for_an_addable_tool():
    assert propose_agent_upgrade(capability="Read mail", reason="r", tool_name=GMAIL) is None

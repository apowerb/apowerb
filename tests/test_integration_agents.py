"""Integration tests for the agents CRUD endpoints."""

import httpx
import pytest
import uuid

pytestmark = pytest.mark.integration


def _unique(name: str) -> str:
    return f"{name}_{uuid.uuid4().hex[:8]}"


VALID_AGENT_PAYLOAD: dict = {
    "agent_name": "integration-test-agent",
    "agent_model": "gpt-4o-mini",
    "agent_description": "Agent created by integration tests",
    "agent_instruction": "You are a test agent. Reply with 'OK'.",
    "agent_type": "llm_agent",
    "agent_tools": [],
}


def _agent_id_from_response(body: dict) -> str:
    """Extract agent_id from create/update response, handling various formats."""
    agent_id = body.get("agent_id") or body.get("id")
    assert agent_id, f"No agent_id in response: {body}"
    return str(agent_id)


class TestCreateAgent:
    def test_create_agent(
        self,
        base_url: str,
        auth_headers: dict[str, str],
        created_agent_ids: list[str],
    ) -> None:
        response = httpx.post(
            f"{base_url}/api/agents",
            json={**VALID_AGENT_PAYLOAD, "agent_name": _unique("integ-agent")},
            headers=auth_headers,
        )
        assert response.status_code in (200, 201), (
            f"Create failed ({response.status_code}): {response.text}"
        )
        body = response.json()
        agent_id = _agent_id_from_response(body)
        created_agent_ids.append(agent_id)

    def test_create_agent_missing_name(
        self, base_url: str, auth_headers: dict[str, str]
    ) -> None:
        payload = {
            k: v for k, v in VALID_AGENT_PAYLOAD.items() if k != "agent_name"
        }
        response = httpx.post(
            f"{base_url}/api/agents",
            json=payload,
            headers=auth_headers,
        )
        assert response.status_code == 422

    def test_create_agent_missing_model(
        self, base_url: str, auth_headers: dict[str, str]
    ) -> None:
        payload = {
            k: v for k, v in VALID_AGENT_PAYLOAD.items() if k != "agent_model"
        }
        response = httpx.post(
            f"{base_url}/api/agents",
            json=payload,
            headers=auth_headers,
        )
        assert response.status_code == 422


class TestGetAgent:
    def test_get_agent(
        self,
        base_url: str,
        auth_headers: dict[str, str],
        created_agent_ids: list[str],
    ) -> None:
        # Create first
        resp = httpx.post(
            f"{base_url}/api/agents",
            json={**VALID_AGENT_PAYLOAD, "agent_name": _unique("integ-agent")},
            headers=auth_headers,
        )
        assert resp.status_code in (200, 201)
        agent_id = _agent_id_from_response(resp.json())
        created_agent_ids.append(agent_id)

        # Fetch
        get_resp = httpx.get(
            f"{base_url}/api/agents/{agent_id}",
            headers=auth_headers,
        )
        assert get_resp.status_code == 200
        body = get_resp.json()
        assert body.get("agent_name", "").startswith("integ-agent_")


class TestListAgents:
    def test_list_agents(
        self, base_url: str, auth_headers: dict[str, str]
    ) -> None:
        response = httpx.get(
            f"{base_url}/api/agents", headers=auth_headers
        )
        assert response.status_code == 200
        body = response.json()
        assert isinstance(body, list)


class TestUpdateAgent:
    def test_update_agent(
        self,
        base_url: str,
        auth_headers: dict[str, str],
        created_agent_ids: list[str],
    ) -> None:
        # Create
        resp = httpx.post(
            f"{base_url}/api/agents",
            json={**VALID_AGENT_PAYLOAD, "agent_name": _unique("integ-agent")},
            headers=auth_headers,
        )
        assert resp.status_code in (200, 201)
        agent_id = _agent_id_from_response(resp.json())
        created_agent_ids.append(agent_id)

        # Update
        updated_payload = {
            **VALID_AGENT_PAYLOAD,
            "agent_description": "Updated by integration test",
        }
        # The PUT endpoint expects a numeric id (strip "agent" prefix)
        put_resp = httpx.put(
            f"{base_url}/api/agents/{agent_id}",
            json=updated_payload,
            headers=auth_headers,
        )
        assert put_resp.status_code == 200

    def test_update_nonexistent_agent(
        self, base_url: str, auth_headers: dict[str, str]
    ) -> None:
        updated_payload = {
            **VALID_AGENT_PAYLOAD,
            "agent_description": "Should not exist",
        }
        resp = httpx.put(
            f"{base_url}/api/agents/agent9999999",
            json=updated_payload,
            headers=auth_headers,
        )
        assert resp.status_code in (403, 404)


class TestDeleteAgent:
    def test_delete_agent(
        self,
        base_url: str,
        auth_headers: dict[str, str],
    ) -> None:
        # Create
        resp = httpx.post(
            f"{base_url}/api/agents",
            json={**VALID_AGENT_PAYLOAD, "agent_name": _unique("integ-agent")},
            headers=auth_headers,
        )
        assert resp.status_code in (200, 201)
        agent_id = _agent_id_from_response(resp.json())

        # Delete
        del_resp = httpx.delete(
            f"{base_url}/api/agents/{agent_id}",
            headers=auth_headers,
        )
        assert del_resp.status_code == 200

    def test_delete_nonexistent_agent(
        self, base_url: str, auth_headers: dict[str, str]
    ) -> None:
        resp = httpx.delete(
            f"{base_url}/api/agents/agent9999999",
            headers=auth_headers,
        )
        assert resp.status_code in (404, 200)

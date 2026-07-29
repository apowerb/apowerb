"""Integration tests for Hub clone and propagate_api_key features."""

import httpx
import pytest
import uuid

pytestmark = pytest.mark.integration


def _unique(name: str) -> str:
    """Append a short unique suffix to avoid name collisions."""
    return f"{name}_{uuid.uuid4().hex[:8]}"


ORCHESTRATOR_PAYLOAD: dict = {
    "agent_name": "integ-test-orchestrator",
    "agent_model": "gpt-4o-mini",
    "agent_description": "Orchestrator for clone tests",
    "agent_instruction": "You orchestrate sub-agents.",
    "agent_type": "llm_agent",
    "agent_tools": [],
    "sub_agents": [],
}

SUB_AGENT_PAYLOAD: dict = {
    "agent_name": "integ-test-sub-agent",
    "agent_model": "gpt-4o-mini",
    "agent_description": "Sub-agent for clone tests",
    "agent_instruction": "You are a sub-agent.",
    "agent_type": "llm_agent",
    "agent_tools": [],
}


def _agent_id_from_response(body: dict) -> str:
    agent_id = body.get("agent_id") or body.get("id")
    assert agent_id, f"No agent_id in response: {body}"
    return str(agent_id)


def _publish_to_hub(
    base_url: str,
    headers: dict[str, str],
    agent_id: str,
) -> str:
    """Publish an agent to the hub and return the hub_id."""
    resp = httpx.post(
        f"{base_url}/api/hub/publish",
        json={
            "agent_id": agent_id,
            "hub_name": "integ-test-hub-agent",
            "hub_description": "Published for integration test",
            "hub_tags": ["test"],
            "hub_category": "general",
        },
        headers=headers,
    )
    assert resp.status_code in (200, 201), (
        f"Publish failed ({resp.status_code}): {resp.text}"
    )
    body = resp.json()
    hub_id = body.get("hub_id") or body.get("id")
    assert hub_id, f"No hub_id in publish response: {body}"
    return str(hub_id)


class TestCloneFromHub:
    def test_clone_from_hub(
        self,
        base_url: str,
        auth_headers: dict[str, str],
        created_agent_ids: list[str],
    ) -> None:
        # 1. Create a source agent
        resp = httpx.post(
            f"{base_url}/api/agents",
            json={**ORCHESTRATOR_PAYLOAD, "agent_name": _unique("integ-orch")},
            headers=auth_headers,
        )
        assert resp.status_code in (200, 201)
        source_id = _agent_id_from_response(resp.json())
        created_agent_ids.append(source_id)

        # 2. Publish to hub
        hub_id = _publish_to_hub(base_url, auth_headers, source_id)

        # 3. Clone from hub
        clone_resp = httpx.post(
            f"{base_url}/api/hub/clone",
            json={"hub_agent_id": hub_id, "clone_name": "integ-cloned-agent"},
            headers=auth_headers,
        )
        assert clone_resp.status_code in (200, 201), (
            f"Clone failed ({clone_resp.status_code}): {clone_resp.text}"
        )
        clone_body = clone_resp.json()
        cloned_id = _agent_id_from_response(clone_body)
        created_agent_ids.append(cloned_id)

        # 4. Verify cloned agent exists
        get_resp = httpx.get(
            f"{base_url}/api/agents/{cloned_id}",
            headers=auth_headers,
        )
        assert get_resp.status_code == 200

        # 5. Cleanup hub entry
        httpx.delete(f"{base_url}/api/hub/{hub_id}", headers=auth_headers)


class TestClonePreservesSubAgents:
    def test_clone_preserves_sub_agents(
        self,
        base_url: str,
        auth_headers: dict[str, str],
        created_agent_ids: list[str],
    ) -> None:
        # 1. Create a sub-agent (unique name to avoid 409 on clone)
        sub_resp = httpx.post(
            f"{base_url}/api/agents",
            json={**SUB_AGENT_PAYLOAD, "agent_name": _unique("integ-sub")},
            headers=auth_headers,
        )
        assert sub_resp.status_code in (200, 201)
        sub_id = _agent_id_from_response(sub_resp.json())
        created_agent_ids.append(sub_id)

        # 2. Create orchestrator with sub_agents (unique name)
        orch_payload = {
            **ORCHESTRATOR_PAYLOAD,
            "agent_name": _unique("integ-orch"),
            "sub_agents": [sub_id],
        }
        orch_resp = httpx.post(
            f"{base_url}/api/agents",
            json=orch_payload,
            headers=auth_headers,
        )
        assert orch_resp.status_code in (200, 201)
        orch_id = _agent_id_from_response(orch_resp.json())
        created_agent_ids.append(orch_id)

        # 3. Publish orchestrator
        hub_id = _publish_to_hub(base_url, auth_headers, orch_id)

        # 4. Clone
        clone_resp = httpx.post(
            f"{base_url}/api/hub/clone",
            json={"hub_agent_id": hub_id},
            headers=auth_headers,
        )
        assert clone_resp.status_code in (200, 201), (
            f"Clone failed ({clone_resp.status_code}): {clone_resp.text}"
        )
        cloned_id = _agent_id_from_response(clone_resp.json())
        created_agent_ids.append(cloned_id)

        # 5. Verify sub_agents on cloned agent
        get_resp = httpx.get(
            f"{base_url}/api/agents/{cloned_id}",
            headers=auth_headers,
        )
        assert get_resp.status_code == 200
        cloned_body = get_resp.json()
        sub_agents = cloned_body.get("sub_agents")
        assert sub_agents is not None and len(sub_agents) > 0, (
            f"Cloned agent has no sub_agents: {cloned_body}"
        )

        # 6. Cleanup hub
        httpx.delete(f"{base_url}/api/hub/{hub_id}", headers=auth_headers)


class TestPropagateApiKey:
    def test_propagate_api_key(
        self,
        base_url: str,
        auth_headers: dict[str, str],
        created_agent_ids: list[str],
    ) -> None:
        # 1. Create sub-agent (unique name)
        sub_resp = httpx.post(
            f"{base_url}/api/agents",
            json={**SUB_AGENT_PAYLOAD, "agent_name": _unique("integ-prop-sub")},
            headers=auth_headers,
        )
        assert sub_resp.status_code in (200, 201)
        sub_id = _agent_id_from_response(sub_resp.json())
        created_agent_ids.append(sub_id)

        # 2. Create orchestrator with sub-agent (unique name)
        orch_payload = {
            **ORCHESTRATOR_PAYLOAD,
            "agent_name": _unique("integ-prop-orch"),
            "sub_agents": [sub_id],
            "agent_model_params": {
                "model_api_key": "sk-test-propagation-key",
                "model_api_base": "https://api.example.com",
            },
        }
        orch_resp = httpx.post(
            f"{base_url}/api/agents",
            json=orch_payload,
            headers=auth_headers,
        )
        assert orch_resp.status_code in (200, 201)
        orch_id = _agent_id_from_response(orch_resp.json())
        created_agent_ids.append(orch_id)

        # 3. Update with propagate_api_key=true
        update_payload = {
            **orch_payload,
            "propagate_api_key": True,
        }
        put_resp = httpx.put(
            f"{base_url}/api/agents/{orch_id}",
            json=update_payload,
            headers=auth_headers,
        )
        assert put_resp.status_code == 200

        # 4. Verify sub-agent received the key
        sub_get = httpx.get(
            f"{base_url}/api/agents/{sub_id}",
            headers=auth_headers,
        )
        assert sub_get.status_code == 200
        sub_body = sub_get.json()
        sub_params = sub_body.get("agent_model_params") or {}
        propagated_key = sub_params.get("model_api_key")
        # The server encrypts API keys, so we verify the key was propagated
        # (non-empty) rather than matching the plaintext value.
        assert propagated_key and len(propagated_key) > 0, (
            f"API key not propagated to sub-agent: {sub_params}"
        )

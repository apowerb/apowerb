"""Integration tests: clone with custom provider/model override."""

import httpx
import pytest
import uuid

pytestmark = pytest.mark.integration


def _unique(name: str) -> str:
    return f"{name}_{uuid.uuid4().hex[:8]}"


def _agent_id(body: dict) -> str:
    aid = body.get("agent_id") or body.get("id")
    assert aid, f"No agent_id: {body}"
    return str(aid)


def _numeric_id(aid: str) -> str:
    return str(aid).replace("agent", "")


class TestCloneWithCustomProvider:
    """After cloning, user can change the model/provider to anything."""

    def test_update_cloned_agent_with_different_model(
        self, base_url, auth_headers, created_agent_ids,
    ):
        """Clone an agent, then update it with a completely different model."""
        # 1. Create source agent with mistral model
        resp = httpx.post(f"{base_url}/api/agents", json={
            "agent_name": _unique("src-mistral"),
            "agent_model": "mistral/Mistral-Small",
            "agent_description": "Source", "agent_instruction": "Do stuff",
            "agent_type": "llm_agent", "agent_tools": [],
        }, headers=auth_headers)
        assert resp.status_code in (200, 201)
        src_id = _agent_id(resp.json())
        created_agent_ids.append(src_id)

        # 2. Verify source has mistral model
        get_resp = httpx.get(f"{base_url}/api/agents/{_numeric_id(src_id)}", headers=auth_headers)
        assert get_resp.json()["agent_model"] == "mistral/Mistral-Small"

        # 3. Update to openai model with different api key + base
        update_resp = httpx.put(f"{base_url}/api/agents/{_numeric_id(src_id)}", json={
            "agent_name": get_resp.json()["agent_name"],
            "agent_model": "openai/gpt-4o",
            "agent_description": "Source", "agent_instruction": "Do stuff",
            "agent_type": "llm_agent", "agent_tools": [],
            "sub_agents": [],
            "agent_model_params": {
                "model_api_key": "sk-openai-fake-key",
                "model_api_base": "https://api.openai.com/v1",
            },
        }, headers=auth_headers)
        assert update_resp.status_code == 200

        # 4. Verify model changed
        verify = httpx.get(f"{base_url}/api/agents/{_numeric_id(src_id)}", headers=auth_headers)
        assert verify.json()["agent_model"] == "openai/gpt-4o"

    def test_propagate_custom_provider_to_sub_agents(
        self, base_url, auth_headers, created_agent_ids,
    ):
        """Clone sub-agents should receive the custom provider, not the original."""
        # 1. Create sub-agent with mistral
        sub_resp = httpx.post(f"{base_url}/api/agents", json={
            "agent_name": _unique("sub-mistral"),
            "agent_model": "mistral/Mistral-Small",
            "agent_description": "Sub", "agent_instruction": "sub task",
            "agent_type": "llm_agent", "agent_tools": [],
        }, headers=auth_headers)
        assert sub_resp.status_code in (200, 201)
        sub_id = _agent_id(sub_resp.json())
        created_agent_ids.append(sub_id)

        # 2. Create parent with sub-agent
        parent_resp = httpx.post(f"{base_url}/api/agents", json={
            "agent_name": _unique("parent-mistral"),
            "agent_model": "mistral/Mistral-Small",
            "agent_description": "Parent", "agent_instruction": "orchestrate",
            "agent_type": "llm_agent", "agent_tools": [],
            "sub_agents": [sub_id],
        }, headers=auth_headers)
        assert parent_resp.status_code in (200, 201)
        parent_id = _agent_id(parent_resp.json())
        created_agent_ids.append(parent_id)

        # 3. Update parent with Google model + propagate
        parent_data = httpx.get(
            f"{base_url}/api/agents/{_numeric_id(parent_id)}", headers=auth_headers,
        ).json()
        update_resp = httpx.put(f"{base_url}/api/agents/{_numeric_id(parent_id)}", json={
            "agent_name": parent_data["agent_name"],
            "agent_model": "google/gemini-2.0-flash",
            "agent_description": parent_data["agent_description"],
            "agent_instruction": parent_data["agent_instruction"],
            "agent_type": parent_data["agent_type"],
            "agent_tools": parent_data.get("agent_tools", []),
            "sub_agents": parent_data.get("sub_agents", []),
            "agent_model_params": {
                "model_api_key": "AIza-fake-google-key",
                "model_api_base": "https://generativelanguage.googleapis.com/v1",
            },
            "propagate_api_key": True,
        }, headers=auth_headers)
        assert update_resp.status_code == 200
        body = update_resp.json()
        assert "propagated_to" in body, f"No propagation in response: {body}"

        # 4. Verify sub-agent got the Google model, NOT mistral
        sub_data = httpx.get(
            f"{base_url}/api/agents/{_numeric_id(sub_id)}", headers=auth_headers,
        ).json()
        assert sub_data["agent_model"] == "google/gemini-2.0-flash", (
            f"Sub-agent still has {sub_data['agent_model']}, expected google/gemini-2.0-flash"
        )

    def test_model_field_accepts_any_provider_format(
        self, base_url, auth_headers, created_agent_ids,
    ):
        """The model field should accept any provider/model format."""
        providers = [
            "openai/gpt-4o",
            "anthropic/claude-sonnet-4-20250514",
            "google/gemini-2.0-flash",
            "mistral/Mistral-Small-3.2-24B-Instruct-2506",
            "groq/llama-3.3-70b-versatile",
            "custom-provider/my-custom-model",
        ]
        for model in providers:
            resp = httpx.post(f"{base_url}/api/agents", json={
                "agent_name": _unique("provider-test"),
                "agent_model": model,
                "agent_description": "test", "agent_instruction": "test",
                "agent_type": "llm_agent", "agent_tools": [],
            }, headers=auth_headers)
            assert resp.status_code in (200, 201), f"Failed for model={model}: {resp.text}"
            aid = _agent_id(resp.json())
            created_agent_ids.append(aid)

            # Verify it was stored
            get_resp = httpx.get(
                f"{base_url}/api/agents/{_numeric_id(aid)}", headers=auth_headers,
            )
            assert get_resp.json()["agent_model"] == model

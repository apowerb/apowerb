"""Building an agent leaves its model key and endpoint out of ``os.environ``.

The model gets them as explicit ``api_key``/``api_base``. litellm falls back
to the process env (``MISTRAL_API_BASE``, ``ANTHROPIC_API_KEY``...) when a
call does not carry them, so whatever a build writes there is reused by later
calls of other agents in the same worker.
"""
from __future__ import annotations

import json
import os

import pytest

_ENV_KEYS = ("MISTRAL_API_KEY", "MISTRAL_API_BASE", "ANTHROPIC_API_KEY", "OVHCLOUD_API_KEY")

_ROWS = {
    5101: ("mistral/mistral-large-latest",
           {"model_api_key": "key-a", "model_api_base": "https://endpoint-a.example/v1"}),
    5102: ("mistral/mistral-small-latest", {"model_api_key": "key-b"}),
}


def _row(agent_id):
    model, params = _ROWS[agent_id]
    return {
        "agent_id": agent_id, "agent_name": f"A{agent_id}", "agent_model": model,
        "agent_model_params": json.dumps(params), "agent_description": "",
        "agent_instruction": "Réponds.", "agent_tools": "[]", "agent_type": "base",
        "sub_agents": "[]", "owner_id": f"owner{agent_id}@example.com",
        "organization_id": "test", "project_id": "test", "memory_enabled": "false",
        "artifacts_enabled": "false", "mcp_servers": None, "guardrails_config": None,
    }


@pytest.fixture
def build(monkeypatch):
    from apowerb.core.agent_helpers import agent_utils, extras_loader, llm_model_builder

    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    plain = lambda d, values_to_decrypt: d  # noqa: E731 - keys stored in clear here
    monkeypatch.setattr(agent_utils, "decrypt_value_in_dict", plain)
    monkeypatch.setattr(llm_model_builder, "decrypt_value_in_dict", plain)
    for name in ("_inject_tool_config_env", "_owner_category_params"):
        if hasattr(extras_loader, name):
            monkeypatch.setattr(extras_loader, name, lambda *a, **k: {})
    monkeypatch.setattr(agent_utils, "get_agent_details",
                        lambda agent_id, **_: _row(int(agent_id)))
    return lambda agent_id: agent_utils.to_agent(f"agent{agent_id}")


def test_build_leaves_model_key_and_endpoint_out_of_env(build):
    agent = build(5101)

    assert {k: os.environ.get(k) for k in _ENV_KEYS} == dict.fromkeys(_ENV_KEYS)
    assert agent.model._additional_args.get("api_key") == "key-a"


def test_cached_agent_does_not_pick_up_a_later_build_endpoint(build):
    agent_b = build(5102)
    build(5101)

    # litellm's mistral route uses MISTRAL_API_BASE when the call has none.
    args = agent_b.model._additional_args
    effective_base = args.get("api_base") or os.environ.get("MISTRAL_API_BASE")
    assert effective_base != "https://endpoint-a.example/v1"
    assert args.get("api_key") == "key-b"

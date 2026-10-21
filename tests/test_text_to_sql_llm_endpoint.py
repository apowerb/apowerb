"""The SQL tools call the agent's model the way the agent itself does.

They called litellm with the raw agent model and no api_version. For an Azure
agent with a model_api_base (``azure/gpt-5.4`` + an OpenAI-compatible
endpoint), the agent goes through ``openai/gpt-5.4`` at that endpoint
(build_litellm_model), while tool_text_to_sql went through litellm's Azure
deployment route and got ``NotFoundError - Resource not found``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import apowerb.core.agent_helpers as agent_helpers
import apowerb.tools_store.portfolio.text_to_sql as t2s

SCHEMA = {"schema": "public", "db_type": "postgresql", "tables": {}}
AZURE_BASE = "https://example.openai.azure.com/openai/v1"

MODELS = {
    7: ("azure/gpt-5.4", {
        "model_api_key": "key-azure",
        "model_api_base": AZURE_BASE,
        "model_api_version": "2024-12-01-preview",
    }),
    8: ("gemini/gemini-2.5-flash", {"model_api_key": "key-gemini"}),
    9: ("azure/gpt-4.1-mini", {"model_api_key": "key-az", "api_version": "2025-01-01-preview"}),
    10: ("mistral/mistral-large-latest", {"model_api_key": "key-mistral"}),
    11: ("mistral/Mistral-Small-3.2-24B-Instruct-2506", {"model_api_key": "key-ovh"}),
}


@pytest.fixture(autouse=True)
def agents(monkeypatch):
    monkeypatch.setattr(
        agent_helpers, "load_agent_model_params", lambda agent_id: MODELS[agent_id]
    )


def _completion():
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = "SELECT 1"
    return patch.object(t2s.litellm, "completion", return_value=resp)


def _sql_call_kwargs(agent_name: str) -> dict:
    with _completion() as completion:
        t2s._generate_sql_with_llm("How many rows?", SCHEMA, agent_name)
    assert completion.called
    return completion.call_args.kwargs


def test_azure_agent_with_endpoint_goes_through_the_openai_compat_route():
    kwargs = _sql_call_kwargs("agent7")
    assert kwargs["model"] == "openai/gpt-5.4"
    assert kwargs["api_base"] == AZURE_BASE
    assert kwargs["api_key"] == "key-azure"
    assert kwargs["api_version"] == "2024-12-01-preview"


def test_agent_without_endpoint_keeps_its_model_and_sends_no_api_version():
    kwargs = _sql_call_kwargs("agent8")
    assert kwargs["model"] == "gemini/gemini-2.5-flash"
    assert kwargs["api_base"] is None
    assert "api_version" not in kwargs


def test_api_version_alias_reaches_litellm_on_the_azure_route():
    kwargs = _sql_call_kwargs("agent9")
    assert kwargs["model"] == "azure/gpt-4.1-mini"
    assert kwargs["api_version"] == "2025-01-01-preview"


@pytest.mark.parametrize("agent_id", sorted(MODELS))
def test_sql_tools_and_agent_share_the_model_route(agent_id, monkeypatch):
    import apowerb.core.agent_helpers.llm_model_builder as builder

    monkeypatch.setattr(builder, "decrypt_value_in_dict", lambda d, values_to_decrypt: d)

    model, params = MODELS[agent_id]
    agent_llm = builder.build_litellm_model({"agent_model": model, "agent_model_params": dict(params)}, None)
    assert _sql_call_kwargs(f"agent{agent_id}")["model"] == agent_llm.model

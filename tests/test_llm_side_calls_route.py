"""Side LLM calls (session title, JSON repair) reach the model like the agent.

They rebuilt the route by hand: ``azure_ai/`` on an api_base became
``openai/``, ``api_version`` was dropped, the title call sent no ``api_key``
for Gemini/Anthropic/Azure without an api_base, and every lowercase
``mistral/`` id went to the OVH endpoint. ``litellm_call_kwargs`` is the one
route, shared with ``build_litellm_model``.
"""

from __future__ import annotations

import asyncio
import types

import pytest
from pydantic import BaseModel

import apowerb.core.agent_helpers.llm_model_builder as builder
from apowerb.core.agent_helpers import callbacks
from apowerb.helpers import title_generator as tg

BASE = "https://example.services.ai.azure.com/models"
MODELS = {
    "azure_ai_base": ("azure_ai/gpt-4.1-mini", {"model_api_key": "k1", "model_api_base": BASE}),
    "azure_base": ("azure/gpt-5.4", {"model_api_key": "k2", "model_api_base": BASE,
                                     "model_api_version": "2024-12-01-preview"}),
    "azure_no_base": ("azure/gpt-4.1", {"model_api_key": "k3", "api_version": "2025-01-01-preview"}),
    "gemini": ("gemini/gemini-2.5-flash", {"model_api_key": "k4"}),
    "mistral_api": ("mistral/mistral-large-latest", {"model_api_key": "k5"}),
    "mistral_ovh": ("mistral/Mistral-Small-3.2-24B-Instruct-2506", {"model_api_key": "k6"}),
}


@pytest.mark.parametrize("case", sorted(MODELS))
def test_shared_route_matches_the_agent(case, monkeypatch):
    monkeypatch.setattr(builder, "decrypt_value_in_dict", lambda d, values_to_decrypt: d)
    model, params = MODELS[case]
    agent_llm = builder.build_litellm_model({"agent_model": model, "agent_model_params": dict(params)}, None)
    kwargs = builder.litellm_call_kwargs(model, params)
    assert kwargs["model"] == agent_llm.model
    assert kwargs.get("api_key") == params["model_api_key"]
    version = params.get("model_api_version") or params.get("api_version")
    assert kwargs.get("api_version") == version
    if not version:
        assert "api_version" not in kwargs


def _title_kwargs(case, monkeypatch) -> dict:
    monkeypatch.setattr(tg, "load_agent_model_params", lambda _id: MODELS[case])
    seen = {}

    async def fake(**kwargs):
        seen.update(kwargs)
        msg = types.SimpleNamespace(content="Titre")
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])

    import litellm
    monkeypatch.setattr(litellm, "acompletion", fake, raising=False)
    asyncio.run(tg.generate_session_title("Analyse mes ventes de juin", agent_id="agent7"))
    return seen


def test_title_keeps_the_azure_ai_prefix_on_an_endpoint(monkeypatch):
    kwargs = _title_kwargs("azure_ai_base", monkeypatch)
    assert kwargs["model"] == "azure_ai/gpt-4.1-mini"
    assert kwargs["api_base"] == BASE


def test_title_sends_the_agent_key_without_an_endpoint(monkeypatch):
    assert _title_kwargs("gemini", monkeypatch)["api_key"] == "k4"


def test_title_sends_the_azure_api_version(monkeypatch):
    kwargs = _title_kwargs("azure_no_base", monkeypatch)
    assert kwargs["api_version"] == "2025-01-01-preview"
    assert kwargs["api_key"] == "k3"


def test_title_sends_a_mistral_api_id_to_mistral(monkeypatch):
    kwargs = _title_kwargs("mistral_api", monkeypatch)
    assert kwargs["model"] == "mistral/mistral-large-latest"
    assert "api_base" not in kwargs


class _Out(BaseModel):
    answer: str


def _repair_kwargs(case, monkeypatch) -> dict:
    model, params = MODELS[case]
    seen = {}

    async def fake(**kwargs):
        seen.update(kwargs)
        msg = types.SimpleNamespace(content='{"answer": "ok"}')
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])

    monkeypatch.setattr(callbacks.litellm, "acompletion", fake, raising=False)
    out = asyncio.run(callbacks._attempt_json_repair(
        _Out, "not json", model,
        repair_api_key=params.get("model_api_key"),
        repair_api_base=params.get("model_api_base"),
        repair_api_version=params.get("model_api_version") or params.get("api_version"),
    ))
    assert out == _Out(answer="ok")
    return seen


def test_repair_keeps_the_azure_ai_prefix_on_an_endpoint(monkeypatch):
    assert _repair_kwargs("azure_ai_base", monkeypatch)["model"] == "azure_ai/gpt-4.1-mini"


def test_repair_sends_the_azure_api_version(monkeypatch):
    assert _repair_kwargs("azure_no_base", monkeypatch)["api_version"] == "2025-01-01-preview"

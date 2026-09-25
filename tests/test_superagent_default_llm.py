"""SuperAgent templates offer the server's default LLM when it serves one.

Every template hard-codes an Anthropic model. On a server without an
Anthropic key, an agent created with the template's model fails at its
first message ("Missing Anthropic API Key"), whatever the template.
When the server serves ``thaink2/default`` (DEFAULT_LLM_MODEL +
DEFAULT_LLM_API_KEY), the routes serving the picker propose it instead:

  - list and get routes return ``thaink2/default`` as ``agent_model``,
  - the registry itself is not mutated (internal paths still see the
    declared model, and a second call is unaffected by the first),
  - without a default LLM, the declared model is served unchanged.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from apowerb.configs.settings import get_settings
from apowerb.core.agent_helpers.default_llm import DEFAULT_LLM_MODEL_ID
from apowerb.core.superagents import get_superagent_template
from apowerb.routers.superagents import get_template, list_templates

USER = SimpleNamespace(email="someone@example.com")


@pytest.fixture
def default_llm(monkeypatch):
    def _set(model: str, key: str):
        settings = get_settings()
        monkeypatch.setattr(settings, "default_llm_model", model, raising=False)
        monkeypatch.setattr(settings, "default_llm_api_key", key, raising=False)

    return _set


def _list():
    return asyncio.run(list_templates(current_user=USER))


def _get(template_id: str):
    return asyncio.run(get_template(template_id, current_user=USER))


def test_list_offers_default_llm_when_served(default_llm):
    default_llm("mistral/mistral-large-latest", "xxxx")
    templates = _list()
    assert templates
    assert {t["agent_model"] for t in templates} == {DEFAULT_LLM_MODEL_ID}


def test_get_offers_default_llm_when_served(default_llm):
    default_llm("mistral/mistral-large-latest", "xxxx")
    assert _get("jev_decision_agent")["agent_model"] == DEFAULT_LLM_MODEL_ID


def test_registry_keeps_declared_model(default_llm):
    default_llm("mistral/mistral-large-latest", "xxxx")
    _list()
    _get("jev_decision_agent")
    declared = get_superagent_template("jev_decision_agent")["agent_model"]
    assert declared != DEFAULT_LLM_MODEL_ID
    assert declared.startswith("anthropic/")


@pytest.mark.parametrize("model,key", [("", ""), ("mistral/mistral-large-latest", ""), ("", "xxxx")])
def test_declared_model_without_default_llm(default_llm, model, key):
    default_llm(model, key)
    assert _get("jev_decision_agent")["agent_model"].startswith("anthropic/")
    assert DEFAULT_LLM_MODEL_ID not in {t["agent_model"] for t in _list()}


def test_unknown_template_payload_unchanged(default_llm):
    default_llm("mistral/mistral-large-latest", "xxxx")
    assert _get("no_such_template") == {"message": "SuperAgent template not found."}

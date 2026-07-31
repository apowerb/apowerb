"""Validation du provider de modele a la creation/MAJ d'un agent (fail-fast).

Bug live 09/06 : un agent cree avec modele 'ovh/Qwen3.5' (provider 'ovh'
inexistant) etait accepte en base puis levait litellm.BadRequestError "LLM
Provider NOT provided" a CHAQUE tour -> agent muet. On valide a l'ecriture.
"""
import pytest
from apowerb.core.agent_helpers.llm_model_builder import validate_agent_model


def test_known_providers_pass():
    for m in ["gemini/gemini-2.5-flash",
              "mistral/Mistral-Small-3.2-24B-Instruct-2506",
              "anthropic/claude-sonnet-4-5", "openai/gpt-4o", "gpt-4o-mini"]:
        validate_agent_model(m)  # ne doit pas lever


def test_unknown_provider_rejected():
    with pytest.raises(ValueError) as e:
        validate_agent_model("ovh/Qwen3.5")
    assert "provider" in str(e.value).lower()


def test_bare_unknown_and_junk_rejected():
    with pytest.raises(ValueError):
        validate_agent_model("string")
    with pytest.raises(ValueError):
        validate_agent_model("")
    with pytest.raises(ValueError):
        validate_agent_model("   ")


def test_api_base_skips_validation():
    # endpoint OpenAI-compat : le builder force openai/ -> on ne valide pas le provider brut
    validate_agent_model("ovh/Qwen3.5", {"model_api_base": "https://x/v1"})
    validate_agent_model("whatever", {"model_api_base": "https://x/v1"})


def test_api_base_as_json_string():
    # agent_model_params arrive parfois en JSON string
    validate_agent_model("n-importe-quoi", '{"model_api_base": "https://x/v1"}')


def test_params_none_or_garbage_still_validates_model():
    with pytest.raises(ValueError):
        validate_agent_model("ovh/Qwen3.5", None)
    with pytest.raises(ValueError):
        validate_agent_model("ovh/Qwen3.5", "pas du json")


def test_container_agents_skip_validation():
    # sequential/parallel/loop : ADK ignore le modele -> modele vide legitime, pas de rejet
    for t in ("sequential", "parallel", "loop", "Sequential", "LOOP"):
        validate_agent_model("", None, agent_type=t)
        validate_agent_model("ovh/Qwen3.5", None, agent_type=t)


def test_llm_agent_empty_model_still_rejected():
    # un agent LLM (base/llm_agent) DOIT avoir un modele valide
    with pytest.raises(ValueError):
        validate_agent_model("", None, agent_type="llm_agent")
    with pytest.raises(ValueError):
        validate_agent_model("ovh/Qwen3.5", None, agent_type="base")

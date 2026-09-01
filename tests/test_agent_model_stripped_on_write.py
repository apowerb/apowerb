"""What is validated must be what is stored.

Measured on 2026-09-01, apowerb 0.2.8 on the dev VM. An agent was saved with
``agent_model = "ovhcloud/Qwen3.5-9B "`` — one trailing space. A/B on the same
agent, the same key, the same minute:

    with the trailing space -> OvhcloudException: {"error":{"code":"model_not_found",
                               "message":"The model `Qwen3.5-9B ` does not exist"}}
    without it              -> success

``validate_agent_model`` checks ``agent_model.strip()`` while the raw value is
the one persisted, so the save was accepted without a word and every later call
failed. A guard that normalises to verify, but lets the original through, guards
nothing. These tests pin the normalisation to the write boundary.
"""
from apowerb.schema.agent_schema import AgentCreateSchema
from apowerb.core.agent_helpers.llm_model_builder import validate_agent_model


def _agent(**kw):
    base = dict(agent_name="a", agent_model="ovhcloud/Qwen3.5-9B",
                agent_description="d", agent_instruction="i", agent_type="base")
    base.update(kw)
    return AgentCreateSchema(**base)


def test_trailing_space_is_stripped_from_the_model():
    assert _agent(agent_model="ovhcloud/Qwen3.5-9B ").agent_model == "ovhcloud/Qwen3.5-9B"


def test_leading_space_tab_and_newline_are_stripped():
    for raw in ("  gemini/gemini-2.5-flash", "gemini/gemini-2.5-flash\n",
                "\tgemini/gemini-2.5-flash \r\n"):
        assert _agent(agent_model=raw).agent_model == "gemini/gemini-2.5-flash"


def test_inner_characters_are_left_alone():
    """Negative control: only the ends are touched, never the middle."""
    assert _agent(agent_model="openai/gpt 4o ").agent_model == "openai/gpt 4o"


def test_what_validation_sees_is_what_the_schema_stores():
    """The regression itself: the guard used to pass on a value that was not
    the value being written."""
    stored = _agent(agent_model="  anthropic/claude-sonnet-4-5  ").agent_model
    validate_agent_model(stored)  # must not raise
    assert stored == stored.strip()


def test_credentials_are_stripped_too():
    params = {"model_api_key": "sk-abc123\n", "model_api_base": "  https://x/v1 "}
    got = _agent(agent_model="openai/gpt-4o", agent_model_params=params).agent_model_params
    assert got["model_api_key"] == "sk-abc123"
    assert got["model_api_base"] == "https://x/v1"


def test_other_params_are_untouched():
    """Negative control: normalisation is confined to the two credential keys."""
    params = {"temperature": 0.7, "some_label": "  keep  me  ", "model_api_key": " k "}
    got = _agent(agent_model="openai/gpt-4o", agent_model_params=params).agent_model_params
    assert got["temperature"] == 0.7
    assert got["some_label"] == "  keep  me  "
    assert got["model_api_key"] == "k"


def test_absent_or_non_dict_params_do_not_crash():
    assert _agent(agent_model="openai/gpt-4o").agent_model_params is None


def test_the_masked_key_sentinel_survives():
    """``__unchanged__`` is what the API sends back when the key was not
    touched; stripping must leave it recognisable."""
    got = _agent(agent_model="openai/gpt-4o",
                 agent_model_params={"model_api_key": "__unchanged__"}).agent_model_params
    assert got["model_api_key"] == "__unchanged__"

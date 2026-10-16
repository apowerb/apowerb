"""/rag/index-db-nl generates its SQL with the requesting agent's model.

``_generate_sql_with_llm`` used to take whichever agent registered first in
the worker (``next(iter(_state_registry))``): its model, endpoint and key,
not those of ``data.agent_id``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import apowerb.core.agent_helpers as agent_helpers
import apowerb.routers.rag.index_db as index_db
import apowerb.tools_store.portfolio.text_to_sql as t2s
from apowerb.routers.rag.schemas import IndexDbNlRequest

SCHEMA = {"schema": "public", "db_type": "postgresql", "tables": {}}

MODELS = {
    1: ("gemini/gemini-2.5-flash", {"model_api_key": "key-one"}),
    2: ("mistral/mistral-small", {"model_api_key": "key-two"}),
}


def _state(agent_id: int) -> t2s._AgentState:
    model, params = MODELS[agent_id]
    return t2s._AgentState(
        model=model, api_base=None, api_key=params["model_api_key"],
        agent_name=f"agent{agent_id}", db_ok=True, db_type="postgresql",
        db_host=f"db{agent_id}", db_port=5432, db_name=f"name{agent_id}",
        db_user="u", db_password="p", db_schema="public", db_include="",
        schema_samples=False, cache={"k": agent_id}, cache_ts=1.0,
        agent_id=agent_id,
    )


@pytest.fixture
def two_agents(monkeypatch):
    saved = dict(t2s._state_registry)
    t2s._state_registry.clear()
    t2s._state_registry["agent1"] = _state(1)
    t2s._state_registry["agent2"] = _state(2)
    monkeypatch.setattr(
        agent_helpers, "load_agent_model_params", lambda agent_id: MODELS[agent_id]
    )
    yield
    t2s._state_registry.clear()
    t2s._state_registry.update(saved)


def _completion():
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = "SELECT 1"
    return patch.object(t2s.litellm, "completion", return_value=resp)


def test_generation_uses_the_requesting_agents_model_and_key(two_agents):
    with _completion() as completion:
        assert t2s._generate_sql_with_llm("q", SCHEMA, "agent2") == "SELECT 1"

    kwargs = completion.call_args.kwargs
    assert kwargs["model"] == "mistral/mistral-small"
    assert kwargs["api_key"] == "key-two"
    # Same api_base fallback as make_text_to_sql_tools for mistral/ models.
    assert "ovh.net" in kwargs["api_base"]


def test_generation_leaves_registered_agent_states_untouched(two_agents):
    before = {name: (id(s), vars(s).copy()) for name, s in t2s._state_registry.items()}
    with _completion():
        t2s._generate_sql_with_llm("q", SCHEMA, "agent2")
    after = {name: (id(s), vars(s).copy()) for name, s in t2s._state_registry.items()}
    assert after == before


def test_generation_works_for_an_agent_never_built_in_this_worker(two_agents):
    t2s._state_registry.clear()
    with _completion() as completion:
        t2s._generate_sql_with_llm("q", SCHEMA, "agent1")
    assert completion.call_args.kwargs["api_key"] == "key-one"
    assert t2s._state_registry == {}


def test_index_db_nl_steps_passes_the_request_agent(monkeypatch):
    seen = {}

    def fake_generate(question, schema_info, agent_name):
        seen["agent"] = agent_name
        return "SELECT 1"

    monkeypatch.setattr(index_db, "_get_database_schema", lambda: {"tables": {"t": {}}})
    monkeypatch.setattr(index_db, "invalidate_schema_cache", lambda: None)
    monkeypatch.setattr(index_db, "_generate_sql_with_llm", fake_generate)
    monkeypatch.setattr(index_db, "load_from_db_and_index", lambda **kw: {"status": "ok"})

    data = IndexDbNlRequest(agent_id="agent2", nl_description="q", name="n")
    index_db._index_db_nl_steps(data, None)
    assert seen["agent"] == "agent2"

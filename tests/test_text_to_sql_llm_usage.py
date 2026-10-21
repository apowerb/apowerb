"""The SQL tools' own LLM calls must land in ``llm_usage``.

``tool_business_analyst`` plans (up to four calls) and interprets with
``litellm.completion`` called directly, outside ADK: the agent's
``after_model_callback`` never sees those turns. Before this was wired, the
client gauge (a sum of ``llm_usage.total_tokens``) counted only the parent
agent's turn and the Business Analyst looked cheaper than it is.
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import apowerb.core.agent_helpers.usage_recorder as usage_recorder
import apowerb.tools_store.portfolio.text_to_sql as t2s
from apowerb.models import LlmUsage

AGENT = "agent1147"


def _resp(content="ok", prompt=10, completion=5, total=15, cached=3, reasoning=None):
    r = MagicMock()
    r.choices = [MagicMock()]
    r.choices[0].message.content = content
    r.usage = SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
        completion_tokens_details=(
            None if reasoning is None else SimpleNamespace(reasoning_tokens=reasoning)
        ),
    )
    return r


@pytest.fixture
def state():
    s = t2s._AgentState(
        model="gemini/gemini-2.5-flash", api_base=None, api_key="k",
        agent_name=AGENT, db_ok=False, db_type="postgresql", db_host="",
        db_port=5432, db_name="", db_user="", db_password="", db_schema="",
        db_include="", schema_samples=False, cache={}, cache_ts=0.0,
        agent_id=1147, usage_agent_name="Analyste ventes",
        owner_id="owner@example.com", billed_to_thaink2=True,
    )
    t2s._state_registry[AGENT] = s
    yield s
    t2s._state_registry.pop(AGENT, None)


@pytest.fixture
def rows():
    """Every row handed to the persistence layer, validated as an ORM row."""
    captured: list[dict] = []

    async def fake_persist(**fields):
        LlmUsage(**fields)  # an unknown column would raise here
        captured.append(fields)

    with patch.object(usage_recorder, "_persist_usage_row", fake_persist):
        yield captured


async def _drain():
    await asyncio.gather(*list(usage_recorder._pending_writes))


def _call_agent_llm():
    with patch.object(t2s.litellm, "completion", return_value=_resp()):
        return t2s._agent_llm(AGENT, [{"role": "user", "content": "q"}])


def test_agent_llm_writes_one_llm_usage_row(state, rows):
    async def run():
        out = _call_agent_llm()
        await _drain()
        return out

    assert asyncio.run(run()) == "ok"
    assert len(rows) == 1
    row = rows[0]
    assert row["agent_id"] == 1147
    assert row["agent_name"] == "Analyste ventes"
    assert row["owner_id"] == "owner@example.com"
    assert row["model"] == "gemini/gemini-2.5-flash"
    assert row["billed_to_thaink2"] is True
    assert row["input_tokens"] == 10
    assert row["output_tokens"] == 5
    assert row["cached_tokens"] == 3
    assert row["total_tokens"] == 15


def test_row_carries_the_calling_session(state, rows):
    ctx = SimpleNamespace(session=SimpleNamespace(id="sess-42"), invocation_id="inv-7")

    async def run():
        with t2s._tool_call_scope(ctx):
            _call_agent_llm()
        await _drain()

    asyncio.run(run())
    assert rows[0]["session_id"] == "sess-42"
    assert rows[0]["invocation_id"] == "inv-7"


def test_sql_generation_calls_are_counted_too(state, rows):
    schema = {"schema": "public", "db_type": "postgresql", "tables": {}}

    async def run():
        with patch.object(t2s.litellm, "completion", return_value=_resp("SELECT 1")):
            t2s._agent_generate_sql(AGENT, "q", schema)
        await _drain()

    asyncio.run(run())
    assert len(rows) == 1
    assert rows[0]["total_tokens"] == 15


def test_reasoning_tokens_are_not_billed_twice(state, rows):
    # completion_tokens includes reasoning; pricing adds thoughts to output.
    async def run():
        with patch.object(t2s.litellm, "completion",
                          return_value=_resp(completion=250, total=260, reasoning=200)):
            t2s._agent_llm(AGENT, [{"role": "user", "content": "q"}])
        await _drain()

    asyncio.run(run())
    assert (rows[0]["output_tokens"], rows[0]["thoughts_tokens"]) == (50, 200)


def test_index_db_nl_generation_is_not_recorded(state, rows, monkeypatch):
    # /rag/index-db-nl runs in a worker thread: no loop to write the row.
    import apowerb.core.agent_helpers as agent_helpers

    monkeypatch.setattr(agent_helpers, "load_agent_model_params",
                        lambda agent_id: ("gemini/gemini-2.5-flash", {"model_api_key": "k"}))
    schema = {"schema": "public", "db_type": "postgresql", "tables": {}}

    async def run():
        with patch.object(t2s.litellm, "completion", return_value=_resp("SELECT 1")):
            t2s._generate_sql_with_llm("q", schema, AGENT)
        await _drain()

    asyncio.run(run())
    assert rows == []


def test_sync_call_without_event_loop_answers_and_says_the_row_is_lost(state, rows):
    # A worker thread (ADK tool thread pool, not enabled by apowerb) has no
    # running loop, and the DB engine is bound to the main one: the answer
    # must still come back, and the lost row must be logged, not silent.
    with patch.object(usage_recorder.logger, "warning") as warn:
        assert _call_agent_llm() == "ok"
    assert rows == []
    assert any("no running event loop" in str(c.args[0]) for c in warn.call_args_list)


def test_bound_wrappers_receive_tool_context_hidden_from_the_model():
    from google.adk.tools.function_tool import FunctionTool

    with patch(
        "apowerb.core.agent_helpers.load_agent_model_params",
        return_value=("gemini/gemini-2.5-flash", {}),
    ), patch.object(
        t2s, "_load_agent_usage_identity",
        return_value=("Analyste", "o@example.com", False),
    ):
        tools = t2s.make_text_to_sql_tools(AGENT, db_params=None)
    try:
        llm_tools = [fn for fn in tools if fn.__name__ != "tool_get_database_schema"]
        assert len(llm_tools) == 3
        for fn in llm_tools:
            assert "tool_context" in inspect.signature(fn).parameters, fn.__name__
            decl = FunctionTool(fn)._get_declaration()
            props = (decl.parameters.properties or {}) if decl.parameters else {}
            assert "tool_context" not in props, fn.__name__
    finally:
        t2s._state_registry.pop(AGENT, None)


def test_adk_injected_session_reaches_the_row(rows):
    """Through ADK's own FunctionTool: the session it injects is the one
    the explain tool's two LLM calls (generate + explain) are recorded on."""
    from google.adk.tools.function_tool import FunctionTool

    with patch(
        "apowerb.core.agent_helpers.load_agent_model_params",
        return_value=("gemini/gemini-2.5-flash", {}),
    ), patch.object(
        t2s, "_load_agent_usage_identity",
        return_value=("Analyste", "o@example.com", False),
    ):
        tools = {fn.__name__: fn for fn in t2s.make_text_to_sql_tools(AGENT, db_params=None)}
    ctx = SimpleNamespace(
        session=SimpleNamespace(id="sess-adk"), invocation_id="inv-adk",
        tool_confirmation=None,
    )

    async def run():
        with patch.object(t2s, "_agent_cached_schema", return_value={"tables": {}}), \
                patch.object(t2s.litellm, "completion", return_value=_resp("SELECT 1")):
            out = await FunctionTool(tools["tool_text_to_sql_explain"]).run_async(
                args={"question": "q"}, tool_context=ctx,
            )
        await _drain()
        return out

    try:
        out = asyncio.run(run())
    finally:
        t2s._state_registry.pop(AGENT, None)
    assert out["success"] is True, out
    assert [(r["session_id"], r["invocation_id"], r["agent_id"]) for r in rows] == [
        ("sess-adk", "inv-adk", 1147),
    ] * 2

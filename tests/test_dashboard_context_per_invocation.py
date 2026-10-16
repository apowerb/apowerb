"""The linked dashboard and the chat session are per invocation.

The chat router wrote ``AGENT_DASHBOARD_ID`` and ``AGENT_CHAT_SESSION_ID`` to
``os.environ`` before forwarding the run to ADK, and the BI tools read them
back at call time. Two conversations served by the same worker overwrite each
other's value between the router step and the tool call. These tests replay
that sequence with two concurrent invocations.
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest

from apowerb.core import invocation_context as ic
from apowerb.tools_store.portfolio import business_intelligence as bi


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("AGENT_DASHBOARD_ID", raising=False)
    monkeypatch.delenv("AGENT_CHAT_SESSION_ID", raising=False)
    yield


def _callback_context(session_id, state=None):
    return SimpleNamespace(
        invocation_id=f"inv-{session_id}",
        session=SimpleNamespace(id=session_id),
        state=dict(state or {}),
    )


async def _invocation(session_id, state, read):
    # Router step, as on main: the request's context goes to the process env.
    dash = (state or {}).get("dashboard_id")
    if not dash and session_id.startswith("dashboard-chat-"):
        dash = session_id.replace("dashboard-chat-", "")
    if dash:
        os.environ["AGENT_DASHBOARD_ID"] = dash
    os.environ["AGENT_CHAT_SESSION_ID"] = session_id
    # ADK step: the agent starts, then calls a tool after the model answered.
    cb = ic.make_identity_before_agent_callback(
        owner="o@a.test", organization_id="org", project_id="p", agent_id=1
    )
    cb(_callback_context(session_id, state))
    await asyncio.sleep(0.01)  # the other conversation reaches its router step
    return read()


async def test_chat_charts_go_to_the_board_of_their_own_conversation():
    a, b = await asyncio.gather(
        _invocation("session_aaa", None, lambda: bi._chat_dashboard_identity(None)),
        _invocation("session_bbb", None, lambda: bi._chat_dashboard_identity(None)),
    )
    assert a[0].endswith("-session-aaa")
    assert b[0].endswith("-session-bbb")


@pytest.fixture
def fake_dashboard_read(monkeypatch):
    # The tool hands the coroutine to _run_async; returning the read's
    # arguments directly keeps the test about WHICH dashboard is targeted.
    monkeypatch.setattr(
        bi, "_async_get_dashboard_data",
        lambda dashboard_id, owner_email: {"success": True, "dashboard_id": dashboard_id},
    )
    monkeypatch.setattr(bi, "_run_async", lambda result: result)


async def test_dashboard_tools_target_the_dashboard_of_their_own_conversation(fake_dashboard_read):
    seen = {}

    def read():
        return bi.tool_get_dashboard_data()["dashboard_id"]

    seen["a"], seen["b"], seen["c"] = await asyncio.gather(
        _invocation("dashboard-chat-111", None, read),
        _invocation("session_x", {"dashboard_id": "222"}, read),
        _invocation("dashboard-chat-333", None, read),
    )
    assert seen == {"a": "111", "b": "222", "c": "333"}


async def test_a_plain_chat_does_not_inherit_another_conversations_dashboard(fake_dashboard_read):
    plain, _ = await asyncio.gather(
        _invocation("session_plain", None, bi.tool_get_dashboard_data),
        _invocation("dashboard-chat-999", None, lambda: None),
    )
    assert plain["success"] is False
    assert "dashboard_id" not in plain


async def test_real_adk_run_binds_the_state_delta_dashboard_and_session():
    """Same binding through ADK itself: real Runner, stateDelta, callback."""
    from google.adk.agents import LlmAgent
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    seen = {}

    def probe(callback_context):
        seen["dashboard"] = ic.get_dashboard_id()
        seen["session"] = ic.get_chat_session_id()
        # Ends the run before any model call.
        return types.Content(role="model", parts=[types.Part(text="ok")])

    identity = ic.make_identity_before_agent_callback(
        owner="o@a.test", organization_id="org", project_id="p", agent_id=1
    )
    agent = LlmAgent(name="agent1", model="gemini-2.5-flash", before_agent_callback=[identity, probe])
    sessions = InMemorySessionService()
    await sessions.create_session(app_name="agent1", user_id="u", session_id="session_real")
    runner = Runner(app_name="agent1", agent=agent, session_service=sessions)

    async for _ in runner.run_async(
        user_id="u",
        session_id="session_real",
        new_message=types.Content(role="user", parts=[types.Part(text="hi")]),
        state_delta={"dashboard_id": "dash-42"},
    ):
        pass

    assert seen == {"dashboard": "dash-42", "session": "session_real"}

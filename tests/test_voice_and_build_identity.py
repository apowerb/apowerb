"""The agent identity must not travel through ``os.environ``.

Two places wrote it there: ``to_agent`` at build time and the voice router
before registering an agent's tools. Tools read the identity at call time
through :mod:`apowerb.core.invocation_context`, whose getters fall back to the
environment when nothing is bound for the current task. A voice session never
bound anything, so its tools read whichever value the worker's environment
held, and ``setdefault`` meant the first voice session of a worker kept it.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from apowerb.core import invocation_context as ic

_IDENTITY_ENV = ("AGENT_OWNER", "ROOT_AGENT_ID", "AGENT_ID", "AGENT_ORGANIZATION_ID", "AGENT_PROJECT_ID")

AGENTS = {
    1: {"owner_id": "alice@a.test", "organization_id": "org-a", "project_id": "pa"},
    2: {"owner_id": "bob@b.test", "organization_id": "org-b", "project_id": "pb"},
}


@pytest.fixture(autouse=True)
def _clean_identity(monkeypatch):
    for key in _IDENTITY_ENV:
        monkeypatch.delenv(key, raising=False)
    for var in (
        ic._agent_owner_var,
        ic._agent_org_var,
        ic._agent_project_var,
        ic._root_agent_id_var,
        ic._root_invocation_var,
    ):
        var.set(None)
    yield


def _probe():
    """Stands for any owner-scoped tool: reports the identity it acts as."""
    return {
        "owner": ic.get_agent_owner(),
        "root": ic.get_root_agent_id(),
        "org": ic.get_agent_organization_id(),
        "project": ic.get_agent_project_id(),
    }


async def test_two_concurrent_voice_sessions_keep_their_own_identity(monkeypatch):
    from apowerb.core import agent_helpers
    from apowerb.routers import audio_stream
    from apowerb.tools_store import tools_helpers

    def details(agent_id, **_):
        return {**AGENTS[agent_id], "agent_tools": "[]"}

    monkeypatch.setattr(agent_helpers, "get_agent_details", details)
    monkeypatch.setattr(
        tools_helpers, "load_agent_tools_functions", lambda tools, owner_id: (["probe"], [_probe])
    )

    async def voice_session(agent_name):
        # Each websocket connection is its own task, as under uvicorn.
        tools = audio_stream._resolve_agent_tools(agent_name)
        await asyncio.sleep(0.01)  # the other session registers its tools meanwhile
        return tools[0]()

    alice, bob = await asyncio.gather(voice_session("agent1"), voice_session("agent2"))

    assert alice == {"owner": "alice@a.test", "root": "1", "org": "org-a", "project": "pa"}
    assert bob == {"owner": "bob@b.test", "root": "2", "org": "org-b", "project": "pb"}
    assert not any(key in os.environ for key in _IDENTITY_ENV)


def test_to_agent_writes_no_identity_to_the_environment(monkeypatch):
    from apowerb.core.agent_helpers import agent_utils, extras_loader

    def row(agent_id, **_):
        return {
            "agent_id": agent_id, "agent_name": f"A{agent_id}",
            "agent_model": "gemini/gemini-2.5-flash", "agent_model_params": "{}",
            "agent_description": "", "agent_instruction": "Réponds.", "agent_tools": "[]",
            "agent_type": "base", "sub_agents": "[]", "memory_enabled": "false",
            "artifacts_enabled": "false", "mcp_servers": None,
            "guardrails_config": json.dumps({}), **AGENTS[agent_id],
        }

    monkeypatch.setattr(extras_loader, "_owner_category_params", lambda *a, **k: {})
    monkeypatch.setattr(agent_utils, "get_agent_details", row)

    agent_utils.to_agent("agent1")
    agent_utils.to_agent("agent2")

    # A tool run with no invocation bound (voice, a direct call) must not act
    # as the agent built last in the worker.
    assert _probe()["owner"] == ""
    assert not any(key in os.environ for key in _IDENTITY_ENV)

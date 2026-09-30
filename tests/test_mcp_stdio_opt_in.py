"""The stdio MCP transport is opt-in (off by default).

stdio makes the server spawn a local subprocess from an agent-supplied
command; on a shared deployment that is remote code execution by any
authenticated user. These tests assert the gate at both layers -- the
toolset build point (load_mcp_servers, which the scheduler and background
paths also go through) and the save/update routes -- and never spawn anything.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import google.adk.tools.mcp_tool as adk_mcp
from apowerb.core.agent_helpers import mcp_loader

STDIO_CFG = [
    {"name": "local", "transport": "stdio", "command": "echo", "args": ["hi"], "env": {}}
]


class _FakeToolset:
    """Stand-in for McpToolset: records construction, spawns nothing."""

    instances: list = []

    def __init__(self, **kwargs):
        _FakeToolset.instances.append(kwargs)


@pytest.fixture(autouse=True)
def _no_spawn(monkeypatch):
    _FakeToolset.instances = []
    monkeypatch.setattr(adk_mcp, "McpToolset", _FakeToolset)
    monkeypatch.delenv("MCP_STDIO_ENABLED", raising=False)


# -- flag --------------------------------------------------------------------


def test_flag_defaults_off_and_reads_env(monkeypatch):
    monkeypatch.delenv("MCP_STDIO_ENABLED", raising=False)
    assert mcp_loader.stdio_transport_enabled() is False
    monkeypatch.setenv("MCP_STDIO_ENABLED", "true")
    assert mcp_loader.stdio_transport_enabled() is True
    monkeypatch.setenv("MCP_STDIO_ENABLED", "0")
    assert mcp_loader.stdio_transport_enabled() is False


# -- build point (load_mcp_servers) ------------------------------------------


def test_stdio_is_skipped_when_disabled():
    tools: list = []
    mcp_loader.load_mcp_servers(STDIO_CFG, tools)
    assert tools == []  # no toolset appended
    assert _FakeToolset.instances == []  # nothing built, nothing spawned


def test_stdio_is_built_when_enabled(monkeypatch):
    monkeypatch.setenv("MCP_STDIO_ENABLED", "1")
    tools: list = []
    mcp_loader.load_mcp_servers(STDIO_CFG, tools)
    assert len(tools) == 1
    assert len(_FakeToolset.instances) == 1


# -- routes ------------------------------------------------------------------


@pytest.fixture()
def client(monkeypatch):
    from apowerb.auth.dependencies import get_current_user
    from apowerb.routers import tools as tools_router

    monkeypatch.setattr(
        tools_router, "register_tool_config", lambda **kw: {"ok": "saved"}
    )
    monkeypatch.setattr(
        tools_router, "update_tool_config", lambda **kw: {"ok": "updated"}
    )

    app = FastAPI()
    app.include_router(tools_router.router, prefix="/api")

    async def _user():
        return SimpleNamespace(email="tester@example.com", user_id=1)

    app.dependency_overrides[get_current_user] = _user
    return TestClient(app)


def test_save_stdio_is_forbidden_when_disabled(client):
    r = client.post(
        "/api/mcp_configs",
        json={"name": "x", "transport": "stdio", "command": "sh"},
    )
    assert r.status_code == 403


def test_update_stdio_is_forbidden_when_disabled(client):
    r = client.put(
        "/api/mcp_configs/abc",
        json={"transport": "stdio", "command": "sh"},
    )
    assert r.status_code == 403


def test_save_http_config_is_unaffected(client):
    r = client.post(
        "/api/mcp_configs",
        json={"name": "x", "transport": "http", "url": "https://example.com/mcp"},
    )
    assert r.status_code == 200


def test_save_stdio_is_allowed_when_enabled(client, monkeypatch):
    monkeypatch.setenv("MCP_STDIO_ENABLED", "1")
    r = client.post(
        "/api/mcp_configs",
        json={"name": "x", "transport": "stdio", "command": "sh"},
    )
    assert r.status_code == 200

"""MCP server credentials must never reach the logs.

Remote MCP servers authenticate with headers (e.g. ``Authorization``) and with
decrypted ``params`` appended to the URL query string. The loader may log the
server's scheme and host, never those values.
"""

from __future__ import annotations

import logging
import re

import pytest

import google.adk.tools.mcp_tool as adk_mcp
from apowerb.core.agent_helpers import mcp_loader

TOKEN = "tok-SECRET-123"
PARAM_VALUE = "param-SECRET-456"


class _FakeToolset:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(adk_mcp, "McpToolset", _FakeToolset)
    # Values are stored encrypted; the identity stands in for decryption.
    monkeypatch.setattr(
        mcp_loader, "decrypt_value_in_dict", lambda d, values_to_decrypt=None: dict(d)
    )


@pytest.mark.parametrize("transport", ["sse", "http"])
def test_credentials_are_not_logged(transport, caplog):
    cfg = [
        {
            "name": "remote",
            "transport": transport,
            "url": "https://mcp.example.com/mcp",
            "headers": {"Authorization": f"Bearer {TOKEN}"},
            "params": {"api_key": PARAM_VALUE},
        }
    ]
    tools: list = []
    with caplog.at_level(logging.DEBUG):
        mcp_loader.load_mcp_servers(cfg, tools)

    assert len(tools) == 1  # the server is still loaded
    # The scheme and host are still logged, nothing after them.
    assert re.search(r"https://mcp\.example\.com\)", caplog.text)
    assert TOKEN not in caplog.text
    assert PARAM_VALUE not in caplog.text


@pytest.mark.parametrize("transport", ["sse", "http"])
def test_credentials_are_not_logged_when_loading_fails(transport, caplog):
    # A malformed config (headers given as a string) makes the connection
    # parameters reject their input; the error message must not echo it.
    cfg = [
        {
            "name": "remote",
            "transport": transport,
            "url": "https://mcp.example.com/mcp",
            "headers": f"Authorization: Bearer {TOKEN}",
            "params": {"api_key": PARAM_VALUE},
        }
    ]
    tools: list = []
    with caplog.at_level(logging.DEBUG):
        mcp_loader.load_mcp_servers(cfg, tools)

    assert tools == []
    assert "Failed to load MCP server 'remote'" in caplog.text
    assert TOKEN not in caplog.text
    assert PARAM_VALUE not in caplog.text

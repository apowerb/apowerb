"""A tool built from a tool_config reads that config's params when it runs.

Portfolio tools read their settings with ``os.getenv`` at call time. Agents
are cached per worker, so a tool may run long after other agents were built
in the same process: it must see its own config, nothing left behind by a
later build, and leave the process env as it found it.
"""
from __future__ import annotations

import os
import types

import pytest

from apowerb.tools_store import tools_helpers

_KEYS = ("DB_HOST", "DB_SCHEMA")

_CONFIGS = {
    ("tool_config1", "owner-a@example.com"): {"DB_HOST": "host-a"},
    ("tool_config2", "owner-b@example.com"): {"DB_HOST": "host-b", "DB_SCHEMA": "schema-b"},
}


def tool_probe() -> dict:
    """Return the DB settings visible to the tool."""
    return {k: os.environ.get(k) for k in _KEYS}


class _Store:
    def get_tool_expected_params(self, tool_name):
        return [{"key": k, "default": None} for k in _KEYS]


@pytest.fixture
def probe_portfolio(monkeypatch):
    for k in _KEYS:
        monkeypatch.delenv(k, raising=False)

    def fake_load(tool_config_id, owner_id):
        params = _CONFIGS.get((tool_config_id, owner_id))
        return ("probe.tool_probe", dict(params)) if params else (None, None)

    monkeypatch.setattr(tools_helpers, "load_tool_config_params", fake_load)
    monkeypatch.setattr(tools_helpers.tool_manager.__class__, "get_all_tools",
                        lambda self: {"probe": ["probe.tool_probe"]})
    module = types.SimpleNamespace(tool_probe=tool_probe)
    monkeypatch.setattr(tools_helpers, "_import_from_packs", lambda name, attr: module)
    monkeypatch.setattr("apowerb.tools_store.tool_manager.get_tools_store", lambda: _Store())


def _build(tool_config_id, owner):
    names, funcs = tools_helpers.load_agent_tools_functions([tool_config_id], owner_id=owner)
    assert names == ["probe.tool_probe"]
    return funcs[0]


def test_tool_reads_its_own_config_after_another_build(probe_portfolio):
    tool_a = _build("tool_config1", "owner-a@example.com")
    _build("tool_config2", "owner-b@example.com")

    seen = tool_a()

    assert seen["DB_HOST"] == "host-a"
    # Not set by this config: unset, not the value from the other build.
    assert seen["DB_SCHEMA"] is None


def test_build_and_call_leave_the_env_unchanged(probe_portfolio):
    before = {k: os.environ.get(k) for k in _KEYS}

    tool_b = _build("tool_config2", "owner-b@example.com")
    assert {k: os.environ.get(k) for k in _KEYS} == before

    assert tool_b() == {"DB_HOST": "host-b", "DB_SCHEMA": "schema-b"}
    assert {k: os.environ.get(k) for k in _KEYS} == before


def test_wrapped_tool_keeps_name_and_signature(probe_portfolio):
    import inspect

    tool_a = _build("tool_config1", "owner-a@example.com")

    assert tool_a.__name__ == "tool_probe"
    assert inspect.signature(tool_a) == inspect.signature(tool_probe)
    assert tool_a.__doc__ == tool_probe.__doc__


def test_toolset_factory_reads_its_config_when_built(probe_portfolio, monkeypatch):
    def mcp_tool_set_probe():
        return {"host": os.environ.get("DB_HOST")}

    module = types.SimpleNamespace(mcp_tool_set_probe=mcp_tool_set_probe)
    monkeypatch.setattr(tools_helpers, "_import_from_packs", lambda name, attr: module)
    monkeypatch.setattr(tools_helpers.tool_manager.__class__, "get_all_tools",
                        lambda self: {"probe": ["probe.mcp_tool_set_probe"]})
    monkeypatch.setattr(tools_helpers, "load_tool_config_params",
                        lambda tid, owner_id: ("probe.mcp_tool_set_probe", {"DB_HOST": "host-a"}))

    _, funcs = tools_helpers.load_agent_tools_functions(["tool_config1"], owner_id="owner-a@example.com")

    assert funcs == [{"host": "host-a"}]
    assert os.environ.get("DB_HOST") is None


def test_superagent_native_tool_runs_with_the_owner_category_config(probe_portfolio, monkeypatch):
    from apowerb.core.agent_helpers import extras_loader
    from apowerb.core import superagents

    monkeypatch.setattr(superagents, "get_superagent_template",
                        lambda tid: {"recommended_tools": ["probe.tool_probe"]})
    monkeypatch.setattr(extras_loader, "_owner_category_params",
                        lambda cats, owner: {"probe": {"DB_HOST": f"host-{owner}"}})
    names, funcs = [], []
    extras_loader.load_superagent_recommended_tools(
        {"superagent_template_id": "t", "owner_id": "a"}, names, funcs, owner_id="a")
    extras_loader.load_superagent_recommended_tools(
        {"superagent_template_id": "t", "owner_id": "b"}, [], [], owner_id="b")

    assert names == ["probe.tool_probe"]
    assert funcs[0]()["DB_HOST"] == "host-a"
    assert os.environ.get("DB_HOST") is None


def test_bi_tool_sources_share_the_agent_tool_lock():
    from apowerb.bi.data import tool_executor
    from apowerb.tools_store.tool_env import TOOL_ENV_LOCK

    assert tool_executor._ENV_LOCK is TOOL_ENV_LOCK

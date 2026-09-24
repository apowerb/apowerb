"""Regression tests for roadmap#102: GET /tools must expose ``needs_config``.

The UI listed all 114 catalogue tools without knowing which ones are ready to
run versus which ones still need a user-supplied secret/param, and calling
``/tools/{name}/params`` 114 times to find out was too expensive. The fix
adds a ``needs_config`` boolean to every tool entry returned by
``ToolsStore.get_all_tools_with_status()`` (consumed by ``GET /tools``),
computed from the same source of truth as ``get_tool_expected_params``
(parsed ``os.getenv()`` calls) plus OAuth-bootstrap detection for
integrations (Google, Microsoft Outlook/Teams/OneDrive/SharePoint) that
never go through an env var the user fills in themselves.

``get_all_tools()`` itself (category -> list[str]) must stay untouched: it is
also used by the CLI (tests/test_cli_tools.py) and by
``load_agent_tools_functions`` to resolve real callables for an agent, so
changing its shape would break tool resolution in production.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apowerb.tools_store import tool_manager
from apowerb.tools_store.tool_manager import ToolsStore, get_tools_store


@pytest.fixture(autouse=True)
def _clear_caches():
    """The category-level caches must not leak fake sources between tests."""
    tool_manager._module_source.cache_clear()
    tool_manager._category_requires_oauth.cache_clear()
    yield
    tool_manager._module_source.cache_clear()
    tool_manager._category_requires_oauth.cache_clear()


_PORTFOLIO = {
    "no_param_cat": ["no_param_cat.tool_ping"],
    "env_var_cat": ["env_var_cat.tool_query"],
    "oauth_cat": ["oauth_cat.tool_send_mail"],
}

_SOURCES = {
    "no_param_cat": "def tool_ping():\n    return 'pong'\n",
    "env_var_cat": (
        "import os\n\n"
        "def tool_query():\n"
        "    host = os.getenv('SOME_API_HOST', 'localhost')\n"
        "    return host\n"
    ),
    # No os.getenv() at all -- the only signal is the OAuth bootstrap call,
    # exactly like the real onedrive_read/onedrive_write/teams
    # modules that import a `_graph_headers`/`microsoft_auth_headers` helper
    # which itself calls `_ensure_integration_tokens(...)`.
    "oauth_cat": (
        "def tool_send_mail():\n"
        "    _ensure_integration_tokens('outlook')\n"
        "    return {}\n"
    ),
}


def _fake_module_source(category: str):
    return _SOURCES.get(category)


def _with_fake_portfolio():
    return patch.object(
        ToolsStore, "get_categories", return_value=list(_PORTFOLIO)
    ), patch.object(
        ToolsStore, "get_tools_in_category", side_effect=lambda c: _PORTFOLIO[c]
    ), patch(
        "apowerb.core.extensions.registry.registry.overlay_tools", return_value={}
    ), patch.object(
        tool_manager, "_module_source", side_effect=_fake_module_source
    )


def test_tool_without_params_is_not_needs_config():
    p1, p2, p3, p4 = _with_fake_portfolio()
    with p1, p2, p3, p4:
        result = ToolsStore().get_all_tools_with_status()
    assert result["no_param_cat"] == [
        {"name": "no_param_cat.tool_ping", "needs_config": False}
    ]


def test_tool_with_env_var_needs_config():
    p1, p2, p3, p4 = _with_fake_portfolio()
    with p1, p2, p3, p4:
        result = ToolsStore().get_all_tools_with_status()
    assert result["env_var_cat"] == [
        {"name": "env_var_cat.tool_query", "needs_config": True}
    ]


def test_oauth_tool_needs_config_even_without_env_var():
    p1, p2, p3, p4 = _with_fake_portfolio()
    with p1, p2, p3, p4:
        result = ToolsStore().get_all_tools_with_status()
    assert result["oauth_cat"] == [
        {"name": "oauth_cat.tool_send_mail", "needs_config": True}
    ]


def test_additive_only_names_and_categories_are_preserved():
    """get_all_tools() (plain strings, used by the CLI + tool resolution)
    keeps its exact shape; get_all_tools_with_status() only adds fields.
    """
    p1, p2, p3, p4 = _with_fake_portfolio()
    with p1, p2, p3, p4:
        store = ToolsStore()
        plain = store.get_all_tools()
        status = store.get_all_tools_with_status()
    assert plain == _PORTFOLIO
    assert set(status.keys()) == set(plain.keys())
    for category, names in plain.items():
        assert [t["name"] for t in status[category]] == names


# -- real portfolio modules (no mocking) — catches drift in the actual code --


def test_real_category_with_no_env_var_is_not_needs_config():
    store = get_tools_store()
    result = store.get_all_tools_with_status()
    viz_tools = {t["name"]: t["needs_config"] for t in result.get("visualization", [])}
    assert "visualization.tool_visualize_data" in viz_tools
    assert viz_tools["visualization.tool_visualize_data"] is False


def test_real_category_with_env_var_needs_config():
    store = get_tools_store()
    result = store.get_all_tools_with_status()
    db_tools = {t["name"]: t["needs_config"] for t in result.get("database", [])}
    assert db_tools["database.tool_run_sql"] is True


def test_real_oauth_category_needs_config():
    """onedrive_read never calls os.getenv() itself for its credentials --
    it imports `_graph_headers` from onedrive_core, which bootstraps the
    OAuth refresh token. Must still be True.
    """
    store = get_tools_store()
    result = store.get_all_tools_with_status()
    onedrive_tools = {t["name"]: t["needs_config"] for t in result.get("onedrive_read", [])}
    assert onedrive_tools
    assert all(onedrive_tools.values())


@pytest.mark.parametrize("category", ["outlook_mail", "github", "odoo"])
def test_real_integration_category_needs_config(category):
    """These read the user's integrations row through
    fetch_integration_configs (directly, or via microsoft_auth for Outlook)
    and never call _ensure_integration_tokens: they were reported ready.
    """
    store = get_tools_store()
    result = store.get_all_tools_with_status()
    tools = {t["name"]: t["needs_config"] for t in result.get(category, [])}
    assert tools
    assert all(tools.values())


def test_integration_lookup_in_a_helper_counts_as_needs_config():
    sources = {
        "mail_cat": (
            "from apowerb.tools_store.portfolio.mail_auth import headers\n\n"
            "def tool_list_mails():\n"
            "    return headers()\n"
        ),
        "mail_auth": (
            "def headers():\n"
            "    configs = fetch_integration_configs('mail')\n"
            "    return configs\n"
        ),
    }
    with patch.object(tool_manager, "_module_source", side_effect=sources.get):
        assert tool_manager._category_requires_oauth("mail_cat") is True


# -- the route --------------------------------------------------------------


@pytest.fixture()
def client():
    from apowerb.auth.dependencies import get_current_user
    from apowerb.routers import tools as tools_router

    app = FastAPI()
    app.include_router(tools_router.router, prefix="/api")

    async def _user():
        u = MagicMock()
        u.email = "tester@example.com"
        u.user_id = 1
        u.role = "USER"
        return u

    app.dependency_overrides[get_current_user] = _user
    return TestClient(app)


def test_route_default_shape_is_unchanged(client):
    """Existing clients (both UIs, the SDK) read plain tool names."""
    resp = client.get("/api/tools")
    assert resp.status_code == 200
    db_tools = resp.json().get("database", [])
    assert "database.tool_run_sql" in db_tools
    assert all(isinstance(name, str) for name in db_tools)


def test_route_returns_needs_config_per_tool(client):
    resp = client.get("/api/tools", params={"include_status": "true"})
    assert resp.status_code == 200
    body = resp.json()
    db_tools = {t["name"]: t for t in body.get("database", [])}
    assert "database.tool_run_sql" in db_tools
    entry = db_tools["database.tool_run_sql"]
    assert entry["needs_config"] is True
    assert set(entry.keys()) == {"name", "needs_config"}

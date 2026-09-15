"""Tests for the ``apowerb tools`` CLI.

``tools list`` crashed on every install with ``AttributeError: 'str' object has
no attribute 'get'``: ``ToolsStore.get_all_tools()`` returns
``{category: [tool names]}`` and the command iterated it as a list of dicts.
The store's own ``get_all_tools`` runs here, only the portfolio scan is stubbed,
so the command is exercised against the shape it really receives.
"""

from __future__ import annotations

from unittest.mock import patch

from typer.testing import CliRunner

from apowerb.cli.main import app
from apowerb.tools_store.tool_manager import ToolsStore

_PORTFOLIO = {
    "database": ["database.tool_run_sql", "database.mcp_tool_set_postgres"],
    "mail": ["mail.tool_send_mail"],
    "empty": [],
}


def _invoke_with_portfolio(portfolio):
    with patch.object(ToolsStore, "get_categories", return_value=list(portfolio)), patch.object(
        ToolsStore, "get_tools_in_category", side_effect=lambda category: portfolio[category]
    ), patch("apowerb.core.extensions.registry.registry.overlay_tools", return_value={}):
        return CliRunner().invoke(app, ["tools", "list"])


def test_list_prints_every_tool_under_its_category():
    result = _invoke_with_portfolio(_PORTFOLIO)

    assert result.exit_code == 0, result.output
    assert result.exception is None
    out = result.output
    for category in ("database", "mail"):
        assert f"Category: {category}" in out
    for tools in _PORTFOLIO.values():
        for tool in tools:
            assert tool in out
    assert out.index("Category: database") < out.index("database.tool_run_sql") < out.index("Category: mail")
    assert "N/A" not in out


def test_a_category_without_tools_is_not_listed():
    result = _invoke_with_portfolio(_PORTFOLIO)

    assert "Category: empty" not in result.output


def test_no_tools_at_all_says_so():
    result = _invoke_with_portfolio({"empty": []})

    assert result.exit_code == 0, result.output
    assert "No tools found." in result.output

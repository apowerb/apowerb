"""Conftest for unit_scei tests.

Patches DB connection at module level to allow importing th2agent modules
without a live PostgreSQL connection (OVH DB shared pool constraint).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def pytest_configure(config):
    """Patch tool_config_store before any th2agent module is imported."""
    mock_store = MagicMock()
    mock_store.create_table.return_value = None
    mock_store.tool_config_table = MagicMock()
    mock_store.engine = MagicMock()

    # Pre-populate sys.modules so apowerb.tools_store.tools_helpers doesn't connect
    import sys

    mock_tool_config_mod = MagicMock()
    mock_tool_config_mod.ToolConfigStore = MagicMock(return_value=mock_store)

    mock_tools_helpers = MagicMock()
    mock_tools_helpers.tool_config_store = mock_store
    mock_tools_helpers.load_tool_config_params = MagicMock(return_value={})

    mock_database = MagicMock()
    mock_database.make_database_tools = MagicMock(return_value=[])

    sys.modules.setdefault("apowerb.tools_store.tool_config", mock_tool_config_mod)
    sys.modules.setdefault("apowerb.tools_store.tools_helpers", mock_tools_helpers)
    sys.modules.setdefault(
        "apowerb.tools_store.portfolio.database", mock_database
    )

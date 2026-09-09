"""Conftest for tests/usage_recorder — mirrors tests/unit_scei/conftest.py.

Patches DB-eager modules at collection time so importing
apowerb.core.agent_helpers.* does not open a real Postgres connection
(apowerb.tools_store.tools_helpers runs ``tool_config_store.create_table()``
at module import time — a pre-existing repo-wide trap, see
tests/unit_scei/conftest.py for the original occurrence).
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock


def pytest_configure(config):
    if "apowerb.tools_store.tool_config" in sys.modules:
        return

    mock_store = MagicMock()
    mock_store.create_table.return_value = None
    mock_store.tool_config_table = MagicMock()
    mock_store.engine = MagicMock()

    mock_tool_config_mod = MagicMock()
    mock_tool_config_mod.ToolConfigStore = MagicMock(return_value=mock_store)

    mock_tools_helpers = MagicMock()
    mock_tools_helpers.tool_config_store = mock_store
    mock_tools_helpers.load_agent_tools_functions = MagicMock(return_value=([], []))
    mock_tools_helpers.load_tool_config_params = MagicMock(return_value={})

    mock_database = MagicMock()
    mock_database.make_database_tools = MagicMock(return_value=[])

    sys.modules.setdefault("apowerb.tools_store.tool_config", mock_tool_config_mod)
    sys.modules.setdefault("apowerb.tools_store.tools_helpers", mock_tools_helpers)
    sys.modules.setdefault("apowerb.tools_store.portfolio.database", mock_database)

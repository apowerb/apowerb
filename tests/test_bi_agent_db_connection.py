"""The BI tools find the agent's database connection like the agent does.

The agent binds its SQL tools to the first tool_config whose params carry a
database name, whatever the tool it was saved for. ``_agent_db_connection``
also required "database" in the tool name, so a connection saved for
``text_to_sql`` (DB_* keys) answered SQL questions but was never found for
charts: ``tool_create_chart`` saved the chart without a source, which then
failed to render with ``HTTP 422 ... has no valid data source``.
"""

from __future__ import annotations

import contextvars
import json

import pytest

from apowerb.core import agent_helpers
from apowerb.core import invocation_context as ic
from apowerb.tools_store import tools_helpers
from apowerb.tools_store.portfolio import bi_datasets

CONFIGS = {
    "tool_config1": ("tavily_search", {"TAVILY_API_KEY": "x"}),
    "tool_config7": ("text_to_sql", {"DB_TYPE": "postgresql", "DB_HOST": "db", "DB_NAME": "sales"}),
    "tool_config8": ("database", {"host": "db", "database": "crm"}),
}


@pytest.fixture
def agent(monkeypatch):
    def bind(tools):
        monkeypatch.setattr(agent_helpers, "get_agent_details", lambda _id: {
            "agent_tools": json.dumps(tools), "owner_id": "owner@example.com"})

        def lookup():
            ic.bind_agent_identity(owner="owner@example.com", organization_id=None,
                                   project_id=None, agent_id="42", invocation_id="inv")
            return bi_datasets._agent_db_connection()

        # Own context: the bound identity must not leak into other tests.
        return contextvars.copy_context().run(lookup)

    def load(tid, owner_id):
        assert owner_id == "owner@example.com"
        return CONFIGS.get(tid, (None, None))

    monkeypatch.setattr(tools_helpers, "load_tool_config_params", load)
    return bind


def test_connection_saved_for_text_to_sql_is_found(agent):
    found = agent(["tool_config1", "tool_config7"])
    assert found == ("tool_config7", "owner@example.com")


def test_lowercase_database_connection_is_still_found(agent):
    found = agent(["tool_config1", "tool_config8"])
    assert found == ("tool_config8", "owner@example.com")


def test_first_database_connection_wins_like_the_agent(agent):
    found = agent(["tool_config7", "tool_config8"])
    assert found == ("tool_config7", "owner@example.com")


def test_no_database_connection_gives_none(agent):
    found = agent(["tool_config1", "tool_config99"])
    assert found is None

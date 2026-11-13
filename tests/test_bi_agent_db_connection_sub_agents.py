"""Charts find the database connection in a multi-agent run.

The run's root is its first agent (an orchestrator) while the database is
often attached to a sub-agent (the Data Analyst) that runs the SQL. Looking
only at the root's tools found no connection, so ``tool_create_chart`` saved
SQL charts without a source, which failed with ``HTTP 422 ... has no valid
data source``. The calling agent comes first, then the root, then the
root's sub-agents.
"""

from __future__ import annotations

import contextvars
import json

from apowerb.core import agent_helpers
from apowerb.core import invocation_context as ic
from apowerb.tools_store import tools_helpers
from apowerb.tools_store.portfolio import bi_datasets

OWNER = "owner@example.com"
DB = ("database.tool_run_sql", {"DB_HOST": "db", "DB_NAME": "sales"})
DB2 = ("database.tool_run_sql", {"DB_HOST": "db", "DB_NAME": "crm"})
SEARCH = ("tavily_search", {"TAVILY_API_KEY": "x"})


def _lookup(monkeypatch, agents, configs, bindings):
    """``bindings``: agent ids bound in order, as ADK's callbacks do."""
    monkeypatch.setattr(agent_helpers, "get_agent_details", lambda i: {
        "agent_tools": json.dumps(agents[int(i)].get("tools", [])),
        "sub_agents": json.dumps(agents[int(i)].get("subs", [])),
        "owner_id": OWNER})

    def load(tid, owner_id):
        assert owner_id == OWNER
        return configs.get(tid, (None, None))

    monkeypatch.setattr(tools_helpers, "load_tool_config_params", load)

    def run():
        for agent_id in bindings:
            ic.bind_agent_identity(owner=OWNER, organization_id=None, project_id=None,
                                   agent_id=str(agent_id), invocation_id="inv")
        return bi_datasets._agent_db_connection()

    return contextvars.copy_context().run(run)


def test_sub_agent_calling_the_tool_uses_its_own_connection(monkeypatch):
    agents = {1: {"tools": ["tool_config9"], "subs": ["agent2"]}, 2: {"tools": ["tool_config7"]}}
    found = _lookup(monkeypatch, agents, {"tool_config7": DB, "tool_config9": SEARCH}, [1, 2])
    assert found == ("tool_config7", OWNER)


def test_orchestrator_calling_the_tool_finds_its_sub_agent_connection(monkeypatch):
    agents = {1: {"tools": ["tool_config9"], "subs": ["agent2", "agent3"]},
              2: {"tools": []}, 3: {"tools": ["tool_config7"]}}
    found = _lookup(monkeypatch, agents, {"tool_config7": DB, "tool_config9": SEARCH}, [1])
    assert found == ("tool_config7", OWNER)


def test_calling_agent_connection_wins_over_the_root(monkeypatch):
    agents = {1: {"tools": ["tool_config8"], "subs": ["agent2"]}, 2: {"tools": ["tool_config7"]}}
    found = _lookup(monkeypatch, agents, {"tool_config7": DB, "tool_config8": DB2}, [1, 2])
    assert found == ("tool_config7", OWNER)


def test_root_connection_still_wins_over_its_sub_agents(monkeypatch):
    agents = {1: {"tools": ["tool_config8"], "subs": ["agent2"]}, 2: {"tools": ["tool_config7"]}}
    found = _lookup(monkeypatch, agents, {"tool_config7": DB, "tool_config8": DB2}, [1])
    assert found == ("tool_config8", OWNER)


def test_no_connection_anywhere_gives_none(monkeypatch):
    agents = {1: {"tools": ["tool_config9"], "subs": ["agent2"]}, 2: {"tools": [], "subs": ["agent1"]}}
    assert _lookup(monkeypatch, agents, {"tool_config9": SEARCH}, [1]) is None

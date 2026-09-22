"""Câblage ``workflow:<tool_name>`` dans ``tools_helpers.load_agent_tools_functions``.

Un agent qui référence ``workflow:<tool_name>`` dans ses ``agent_tools``
récupère le callable du workflow publié correspondant — filtré par
propriétaire, comme tout ``tool_config{id}``. Les entrées portfolio
existantes (``category.tool_name``) ne sont pas affectées par ce câblage.
"""

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from apowerb.core import workflow_agent_tools as wat
from apowerb.core import workflow_main as wm
from apowerb.core import workflow_triggers as wt
from apowerb.tools_store import tools_helpers

ALICE = "alice@acme.fr"
BOB = "bob@other.fr"


def _agent_tool_graph(tool_name="lookup_order"):
    return {
        "version": 1,
        "nodes": [
            {
                "id": "start",
                "type": "trigger",
                "config": {
                    "kind": "agent_tool",
                    "tool_name": tool_name,
                    "description": "x",
                    "input_schema": [],
                },
            }
        ],
        "edges": [],
    }


def _sqlite_engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def _attach(dbapi_connection, _record):  # pragma: no cover
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS public")

    return engine


@pytest.fixture(autouse=True)
def store(monkeypatch):
    engine = _sqlite_engine()
    monkeypatch.setattr(wm.workflow_store, "engine", engine)
    wm.workflow_store.metadata.create_all(engine)
    monkeypatch.setattr(wt.workflow_trigger_store, "engine", engine)
    wt.workflow_trigger_store.metadata.create_all(engine)
    return engine


def _publish(wf):
    wm.update_workflow(
        wf["workflow_id"],
        owner_id=wf["owner_id"],
        expected_version=wf["version"],
        status="published",
    )


# --- resolve_single_workflow_tool -------------------------------------------


def test_resolve_single_workflow_tool_by_name():
    wf = wm.create_workflow(owner_id=ALICE, name="W", graph=_agent_tool_graph())
    _publish(wf)

    fn = wat.resolve_single_workflow_tool("workflow:lookup_order", owner_id=ALICE)

    assert fn is not None
    assert fn.__name__ == "workflow:lookup_order"


def test_resolve_single_workflow_tool_unknown_name_returns_none():
    assert wat.resolve_single_workflow_tool("workflow:nope", owner_id=ALICE) is None


def test_resolve_single_workflow_tool_wrong_owner_returns_none():
    wf = wm.create_workflow(owner_id=BOB, name="W", graph=_agent_tool_graph())
    _publish(wf)

    assert (
        wat.resolve_single_workflow_tool("workflow:lookup_order", owner_id=ALICE)
        is None
    )


# --- load_agent_tools_functions wiring --------------------------------------


def test_load_agent_tools_functions_resolves_workflow_prefixed_tool():
    wf = wm.create_workflow(owner_id=ALICE, name="W", graph=_agent_tool_graph())
    _publish(wf)

    names, funcs = tools_helpers.load_agent_tools_functions(
        tools=["workflow:lookup_order"], owner_id=ALICE
    )

    assert names == ["workflow:lookup_order"]
    assert len(funcs) == 1


def test_load_agent_tools_functions_skips_unresolvable_workflow_tool():
    names, funcs = tools_helpers.load_agent_tools_functions(
        tools=["workflow:does_not_exist"], owner_id=ALICE
    )

    assert names == []
    assert funcs == []


def test_load_agent_tools_functions_dedupes_repeated_workflow_tool():
    wf = wm.create_workflow(owner_id=ALICE, name="W", graph=_agent_tool_graph())
    _publish(wf)

    names, _funcs = tools_helpers.load_agent_tools_functions(
        tools=["workflow:lookup_order", "workflow:lookup_order"], owner_id=ALICE
    )

    assert names == ["workflow:lookup_order"]

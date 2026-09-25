"""``create_tool`` — un agent fabrique un outil ``workflow:<nom>``.

L'outil écrit un workflow à déclencheur ``agent_tool``, le publie tout de
suite, et le rend ainsi résolvable par les agents du MÊME utilisateur (celui
qui converse, lu dans ``invocation_context``). Il ne crée jamais d'autre
déclencheur (webhook, planification…) et n'écrit rien quand le graphe est
refusé par la validation du moteur.
"""

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from apowerb.core import invocation_context
from apowerb.core import workflow_agent_tools as wat
from apowerb.core import workflow_main as wm
from apowerb.core import workflow_triggers as wt
from apowerb.core.agent_helpers.tool_creator import create_tool

ALICE = "alice@acme.fr"
BOB = "bob@other.fr"


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
    invocation_context.set_current_invoker(ALICE)
    yield engine
    invocation_context.set_current_invoker(None)


def _echo_tool(**overrides):
    args = dict(
        tool_name="echo_city",
        description="Renvoie la ville reçue.",
        input_schema=[{"name": "city", "type": "string"}],
        nodes=[{"id": "out", "type": "output", "config": {"value": "{{start.city}}"}}],
        edges=[{"source": "start", "target": "out"}],
    )
    args.update(overrides)
    return create_tool(**args)


def test_created_tool_is_published_for_the_invoker_only():
    result = _echo_tool()

    assert result["status"] == "created"
    assert result["tool"] == "workflow:echo_city"
    fn = wat.resolve_single_workflow_tool("workflow:echo_city", owner_id=ALICE)
    assert fn is not None
    assert list(fn.__signature__.parameters) == ["city"]
    assert wat.resolve_single_workflow_tool("workflow:echo_city", owner_id=BOB) is None
    [wf] = wm.list_workflows(owner_id=ALICE)
    assert wm.get_workflow(wf["workflow_id"], owner_id=ALICE)["status"] == "published"


def test_invalid_graph_is_reported_and_nothing_is_written():
    result = _echo_tool(
        nodes=[{"id": "call", "type": "http", "config": {"method": "GET"}}],
        edges=[{"source": "start", "target": "call"}],
    )

    assert result["error"] == "invalid_tool"
    assert any("url manquante" in e for e in result["errors"])
    assert wm.list_workflows(owner_id=ALICE) == []


def test_agent_cannot_bring_its_own_trigger():
    """Sinon un agent pourrait publier un webhook public ou une tâche
    planifiée sous couvert de « créer un outil »."""
    result = _echo_tool(
        nodes=[
            {"id": "hook", "type": "trigger", "config": {"kind": "webhook"}},
            {"id": "out", "type": "output", "config": {"value": "x"}},
        ],
        edges=[{"source": "hook", "target": "out"}],
    )

    assert result["error"] == "invalid_tool"
    assert wm.list_workflows(owner_id=ALICE) == []


def test_tool_name_already_used_is_refused():
    assert _echo_tool()["status"] == "created"

    result = _echo_tool()

    assert result["error"] == "invalid_tool"
    assert len(wm.list_workflows(owner_id=ALICE)) == 1


@pytest.mark.parametrize("invoker", [None, "42"])
def test_refused_without_an_email_invoker(invoker):
    """Pas de repli sur la variable globale AGENT_OWNER : sous charge, elle
    peut désigner le propriétaire d'un autre agent."""
    invocation_context.set_current_invoker(invoker)

    result = _echo_tool()

    assert result["error"] == "no_user"
    assert wm.list_workflows(owner_id=ALICE) == []


def test_adk_can_declare_the_tool():
    from google.adk.tools import FunctionTool

    declaration = FunctionTool(create_tool)._get_declaration()

    assert declaration.name == "create_tool"
    # This ADK version publishes the schema as JSON Schema, not ``parameters``.
    schema = declaration.parameters_json_schema
    assert set(schema["required"]) == {
        "tool_name",
        "description",
        "input_schema",
        "nodes",
        "edges",
    }
    assert schema["properties"]["nodes"]["type"] == "array"


def test_every_llm_agent_gets_the_tool(monkeypatch):
    """Auto-injected like notify_user: an agent with no tool configured
    still receives it."""
    from apowerb.core.agent_helpers import agent_utils

    monkeypatch.setattr(
        agent_utils,
        "get_agent_details",
        lambda agent_id, **_: {
            "agent_id": agent_id,
            "agent_name": "plain",
            "agent_type": "base",
            "agent_model": "gemini-2.5-flash",
            "agent_instruction": "Be helpful.",
            "agent_description": "d",
            "agent_tools": "[]",
            "sub_agents": "[]",
            "owner_id": ALICE,
        },
    )

    agent = agent_utils.to_agent("agent9999")

    names = {getattr(t, "__name__", getattr(t, "name", "")) for t in agent.tools}
    assert "create_tool" in names


def test_created_graph_runs_in_the_engine_with_the_tool_arguments():
    """The graph create_tool stores is the one the engine executes when an
    agent calls the tool: run it for real with the call's arguments."""
    import asyncio
    import json

    from apowerb.core import workflow_graph as wg

    _echo_tool()
    [wf] = wm.list_workflows(owner_id=ALICE)
    graph = wm.get_workflow(wf["workflow_id"], owner_id=ALICE)["graph"]

    async def run_agent(agent_id, message):
        return ""

    async def run_tool(tool, args):
        return {}

    async def go():
        events = []
        async for chunk in wg.run_graph(
            wg.WorkflowGraph.model_validate(graph),
            payload={"city": "Metz"},
            run_agent=run_agent,
            run_tool=run_tool,
            cancel_event=asyncio.Event(),
        ):
            events.append(json.loads(chunk[len("data: ") :]))
        return events

    events = asyncio.run(go())
    done = next(e for e in events if e["event"] == "done")
    assert done["output"] == "Metz"

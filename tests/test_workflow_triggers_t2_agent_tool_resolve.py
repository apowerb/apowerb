"""``core.workflow_agent_tools`` — résolution dynamique ``workflow:<tool_name>``.

Un workflow publié, kind ``agent_tool``, devient un outil ``workflow:<name>``
sélectionnable par les agents du MÊME propriétaire. Ce fichier couvre la
RÉSOLUTION (filtrage propriétaire, signature construite depuis
``input_schema``, refus d'un nom de champ invalide) et l'INVOCATION (délégué
à ``workflow_triggers.call_agent_tool``, remplacé par un double ici — son
comportement est testé dans ``test_workflow_triggers_t2_agent_tool_invoke.py``).
"""

import inspect

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from apowerb.core import workflow_agent_tools as wat
from apowerb.core import workflow_main as wm
from apowerb.core import workflow_triggers as wt

ALICE = "alice@acme.fr"
BOB = "bob@other.fr"


def _agent_tool_graph(tool_name="lookup_order", schema=None):
    return {
        "version": 1,
        "nodes": [
            {
                "id": "start",
                "type": "trigger",
                "config": {
                    "kind": "agent_tool",
                    "tool_name": tool_name,
                    "description": "Cherche une commande.",
                    "input_schema": schema
                    if schema is not None
                    else [
                        {
                            "name": "order_id",
                            "type": "string",
                            "description": "identifiant",
                            "required": True,
                        },
                        {
                            "name": "verbose",
                            "type": "boolean",
                            "description": "détails",
                            "required": False,
                        },
                    ],
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


# --- resolve_workflow_agent_tools -------------------------------------------


def test_resolves_a_published_agent_tool_for_its_owner():
    wf = wm.create_workflow(owner_id=ALICE, name="W", graph=_agent_tool_graph())
    _publish(wf)

    names, funcs = wat.resolve_workflow_agent_tools(ALICE)

    assert names == ["workflow:lookup_order"]
    assert len(funcs) == 1
    assert funcs[0].__name__ == "workflow:lookup_order"


def test_ignores_a_draft_workflow():
    wm.create_workflow(owner_id=ALICE, name="W", graph=_agent_tool_graph())
    # jamais publié

    names, funcs = wat.resolve_workflow_agent_tools(ALICE)

    assert names == []
    assert funcs == []


def test_does_not_resolve_another_owners_tool():
    wf = wm.create_workflow(owner_id=BOB, name="W", graph=_agent_tool_graph())
    _publish(wf)

    names, funcs = wat.resolve_workflow_agent_tools(ALICE)

    assert names == []


def test_built_function_signature_matches_input_schema():
    wf = wm.create_workflow(owner_id=ALICE, name="W", graph=_agent_tool_graph())
    _publish(wf)

    _, funcs = wat.resolve_workflow_agent_tools(ALICE)
    sig = inspect.signature(funcs[0])

    assert list(sig.parameters) == ["order_id", "verbose"]
    assert sig.parameters["order_id"].default is inspect.Parameter.empty
    assert sig.parameters["verbose"].default is None


def test_built_function_docstring_carries_description_and_args():
    wf = wm.create_workflow(owner_id=ALICE, name="W", graph=_agent_tool_graph())
    _publish(wf)

    _, funcs = wat.resolve_workflow_agent_tools(ALICE)

    assert "Cherche une commande." in funcs[0].__doc__
    assert "order_id" in funcs[0].__doc__


def test_invalid_field_name_skips_only_that_tool(monkeypatch, caplog):
    # input_schema est validé par workflow_graph (nom non vide) mais PAS
    # comme identifiant Python valide -- la résolution doit refuser de
    # construire CET outil plutôt que de planter tout le chargement.
    wf = wm.create_workflow(
        owner_id=ALICE,
        name="W",
        graph=_agent_tool_graph(
            schema=[
                {
                    "name": "not a valid identifier",
                    "type": "string",
                    "description": "x",
                    "required": True,
                }
            ]
        ),
    )
    _publish(wf)

    names, funcs = wat.resolve_workflow_agent_tools(ALICE)

    assert names == []
    assert funcs == []


# --- invocation (délègue à workflow_triggers.call_agent_tool) --------------


async def test_calling_the_tool_delegates_to_call_agent_tool(monkeypatch):
    wf = wm.create_workflow(owner_id=ALICE, name="W", graph=_agent_tool_graph())
    _publish(wf)

    calls = []

    async def _fake_call_agent_tool(**kwargs):
        calls.append(kwargs)
        return {"run_id": "run-1", "output": {"found": True}}

    monkeypatch.setattr(wat.wt, "call_agent_tool", _fake_call_agent_tool)

    _, funcs = wat.resolve_workflow_agent_tools(ALICE)
    result = await funcs[0](order_id="42", verbose=True)

    assert result == {"run_id": "run-1", "output": {"found": True}}
    assert calls[0]["workflow_id"] == wf["workflow_id"]
    assert calls[0]["owner_id"] == ALICE
    assert calls[0]["tool_name"] == "lookup_order"
    assert calls[0]["arguments"] == {"order_id": "42", "verbose": True}
    assert calls[0]["prior_chain"] == []

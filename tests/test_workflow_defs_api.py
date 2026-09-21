"""API /api/workflows/defs et branchement de production des nœuds."""

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from apowerb.core.workflow_engine import WorkflowUserError
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from apowerb.core import workflow_graph as wg
from apowerb.core import workflow_main as wm
from apowerb.core import workflow_runtime as rt

ALICE, BOB = "alice@acme.fr", "bob@other.fr"
GRAPH = {
    "version": 1,
    "nodes": [
        {"id": "start", "type": "trigger"},
        {
            "id": "t",
            "type": "tool",
            "config": {"tool": "x.lookup", "args": {"id": "{{start.id}}"}},
        },
        {"id": "a", "type": "agent", "config": {"agent_id": "agent1"}},
    ],
    "edges": [{"source": "start", "target": "t"}, {"source": "t", "target": "a"}],
}


def _user(email):
    u = MagicMock()
    u.email, u.user_id, u.role = email, 1, "USER"
    return u


@pytest.fixture()
def client(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def _attach(dbapi_connection, _record):  # pragma: no cover
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS public")

    monkeypatch.setattr(wm.workflow_store, "engine", engine)
    wm.workflow_store.metadata.create_all(engine)

    from apowerb.auth.dependencies import get_current_user
    from apowerb.routers import workflow_defs

    who = {"email": ALICE}
    app = FastAPI()
    app.include_router(workflow_defs.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: _user(who["email"])
    c = TestClient(app)
    c.who = who
    return c


def test_crud_round_trip_and_owner_isolation(client):
    r = client.post("/api/workflows/defs", json={"name": "Tri", "graph": GRAPH})
    assert r.status_code == 201
    wid = r.json()["workflow_id"]
    got = client.get(f"/api/workflows/defs/{wid}").json()
    assert got["validation"] == {"valid": True, "errors": []}
    assert [w["workflow_id"] for w in client.get("/api/workflows/defs").json()] == [wid]

    client.who["email"] = BOB
    assert client.get(f"/api/workflows/defs/{wid}").status_code == 404
    assert (
        client.put(
            f"/api/workflows/defs/{wid}", json={"expected_version": 1, "name": "x"}
        ).status_code
        == 404
    )
    assert client.delete(f"/api/workflows/defs/{wid}").status_code == 404
    assert client.post(f"/api/workflows/defs/{wid}/run", json={}).status_code == 404

    client.who["email"] = ALICE
    assert client.delete(f"/api/workflows/defs/{wid}").status_code == 204
    assert client.get(f"/api/workflows/defs/{wid}").status_code == 404


def test_conflict_is_409_with_current_version(client):
    wid = client.post(
        "/api/workflows/defs", json={"name": "Tri", "graph": GRAPH}
    ).json()["workflow_id"]
    assert (
        client.put(
            f"/api/workflows/defs/{wid}", json={"expected_version": 1, "name": "A"}
        ).status_code
        == 200
    )
    r = client.put(
        f"/api/workflows/defs/{wid}", json={"expected_version": 1, "name": "B"}
    )
    assert r.status_code == 409 and r.json()["detail"]["current_version"] == 2


def test_validate_and_invalid_structure(client):
    bad = {"version": 1, "nodes": [{"id": "a", "type": "agent"}], "edges": []}
    r = client.post("/api/workflows/defs/validate", json={"graph": bad})
    assert r.json()["valid"] is False and "agent_id" in r.json()["errors"][0]
    r = client.post(
        "/api/workflows/defs",
        json={"name": "x", "graph": {"nodes": [{"id": "!", "type": "zzz"}]}},
    )
    assert r.status_code == 422


def test_run_streams_graph_events_through_the_shared_run_machinery(client, monkeypatch):
    from apowerb.core import run_main
    import apowerb.core.run_gate as gate

    started, finished = [], []
    monkeypatch.setattr(
        run_main, "start_run", lambda **kw: started.append(kw) or kw["run_id"]
    )
    monkeypatch.setattr(
        run_main,
        "finish_run",
        lambda run_id, status, error_message=None: finished.append(status),
    )

    async def _plan(owner):
        return None

    monkeypatch.setattr(gate, "resolve_owner_plan", _plan)
    calls = []

    def _bindings(owner, plan):
        async def run_agent(agent_id, message):
            calls.append(("agent", owner, agent_id, message))
            return {"answer": 42}

        async def run_tool(tool, args):
            calls.append(("tool", owner, tool, args))
            return {"row": args["id"]}

        return run_agent, run_tool

    monkeypatch.setattr(rt, "bindings_for", _bindings)
    wid = client.post(
        "/api/workflows/defs", json={"name": "Tri", "graph": GRAPH}
    ).json()["workflow_id"]
    r = client.post(f"/api/workflows/defs/{wid}/run", json={"payload": {"id": "B7"}})
    assert r.status_code == 200
    events = [
        json.loads(line[6:])
        for line in r.text.split("\n\n")
        if line.startswith("data: ")
    ]
    assert events[0]["event"] == "run_started"
    assert events[-1] == {"event": "done", "output": {"answer": 42}}
    assert calls == [
        ("tool", ALICE, "x.lookup", {"id": "B7"}),
        ("agent", ALICE, "agent1", json.dumps({"row": "B7"})),
    ]
    assert started[0]["config"] == {
        "workflow_id": wid,
        "version": 1,
        "payload": {"id": "B7"},
    }
    assert finished == ["success"]


def test_run_refuses_an_invalid_graph_before_anything(client, monkeypatch):
    bad = {"version": 1, "nodes": [{"id": "a", "type": "agent"}], "edges": []}
    wid = client.post("/api/workflows/defs", json={"name": "x", "graph": bad}).json()[
        "workflow_id"
    ]
    monkeypatch.setattr(
        rt, "bindings_for", lambda *a: pytest.fail("ne doit pas être appelé")
    )
    r = client.post(f"/api/workflows/defs/{wid}/run", json={})
    assert r.status_code == 422


# --- Branchement de production -----------------------------------------------


def test_agent_of_another_owner_is_not_found(monkeypatch):
    import apowerb.core.agent_helpers as helpers

    monkeypatch.setattr(
        helpers,
        "get_agent_details",
        lambda agent_id, **_: {"owner_id": BOB} if agent_id == 9 else {},
    )
    assert rt.check_agent_owner("agent9", BOB) == "agent9"
    with pytest.raises(wg.GraphError, match="introuvable"):
        rt.check_agent_owner("agent9", ALICE)
    with pytest.raises(wg.GraphError, match="introuvable"):
        rt.check_agent_owner("12", ALICE)
    with pytest.raises(wg.GraphError, match="invalide"):
        rt.check_agent_owner("agent9; drop", ALICE)


def test_resolve_tool_scopes_to_owner_and_disambiguates(monkeypatch):
    import apowerb.tools_store.tools_helpers as th

    seen = []

    def one(**kw):
        return "one"

    def two(**kw):
        return "two"

    def _load(tools, owner_id):
        seen.append((tools, owner_id))
        return ["outlook.send", "outlook.read"], [one, two]

    monkeypatch.setattr(th, "load_agent_tools_functions", _load)
    assert rt.resolve_tool("tool_config3:read", ALICE) is two
    assert seen[-1] == (["tool_config3"], ALICE)
    with pytest.raises(wg.GraphError, match="plusieurs"):
        rt.resolve_tool("tool_config3", ALICE)
    with pytest.raises(wg.GraphError, match="introuvable"):
        rt.resolve_tool("tool_config3:nope", ALICE)


def test_call_tool_runs_sync_tools_in_a_thread_and_refuses_agent_only_tools():
    import threading

    main = threading.get_ident()

    def sync_tool(x):
        return {"x": x, "other_thread": threading.get_ident() != main}

    async def async_tool(x):
        return x * 2

    def needs_ctx(x, tool_context):
        return x

    assert asyncio.run(rt.call_tool(sync_tool, {"x": 1})) == {
        "x": 1,
        "other_thread": True,
    }
    assert asyncio.run(rt.call_tool(async_tool, {"x": 2})) == 4
    with pytest.raises(wg.GraphError, match="contexte"):
        asyncio.run(rt.call_tool(needs_ctx, {"x": 1}))


def _record_outcomes(monkeypatch):
    from apowerb.core import run_main
    import apowerb.core.run_gate as gate

    finished = []
    monkeypatch.setattr(run_main, "start_run", lambda **kw: kw["run_id"])
    monkeypatch.setattr(
        run_main,
        "finish_run",
        lambda run_id, status, error_message=None: finished.append(
            (status, error_message)
        ),
    )

    async def _plan(owner):
        return None

    monkeypatch.setattr(gate, "resolve_owner_plan", _plan)
    return finished


def test_a_run_that_fails_midway_is_recorded_as_an_error(client, monkeypatch):
    # Mesuré en e2e (21/09) : le moteur signale l'échec par un événement
    # ``error`` et non une exception ; le run était consigné « success ».
    finished = _record_outcomes(monkeypatch)

    def _bindings(owner, plan):
        async def run_agent(agent_id, message):
            raise WorkflowUserError("quota dépassé")

        async def run_tool(tool, args):
            return {"row": 1}

        return run_agent, run_tool

    monkeypatch.setattr(rt, "bindings_for", _bindings)
    wid = client.post(
        "/api/workflows/defs", json={"name": "Tri", "graph": GRAPH}
    ).json()["workflow_id"]
    r = client.post(f"/api/workflows/defs/{wid}/run", json={"payload": {"id": "B7"}})
    events = [
        json.loads(line[6:])
        for line in r.text.split("\n\n")
        if line.startswith("data: ")
    ]
    assert events[-1] == {"event": "error", "detail": "quota dépassé"}
    assert finished == [("error", "quota dépassé")]


def test_a_run_cancelled_by_the_engine_is_recorded_as_cancelled(monkeypatch):
    finished = _record_outcomes(monkeypatch)
    from apowerb.routers import workflows as wf_router

    async def _runner(cancel_event):
        yield 'data: {"event": "cancelled"}\n\n'

    async def _go():
        resp = wf_router._streaming_run("run-c", [], None, ALICE, runner=_runner)
        return [c async for c in resp.body_iterator]

    asyncio.run(_go())
    assert finished == [("cancelled", None)]

"""Route « Tester jusqu'ici » : POST /workflows/defs/{id}/run avec ``until``."""

import json
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from apowerb.core import run_main
from apowerb.core import workflow_main as wm
from apowerb.core import workflow_runtime as rt
from apowerb.core import workflow_triggers as wt

ALICE = "alice@acme.fr"
GRAPH = {
    "version": 1,
    "nodes": [
        {"id": "start", "type": "trigger"},
        {"id": "t", "type": "tool", "config": {"tool": "x.lookup", "args": {"id": "{{start.id}}"}}},
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

    for store in (wm.workflow_store, wt.workflow_trigger_store, run_main.run_store):
        monkeypatch.setattr(store, "engine", engine)
        store.metadata.create_all(engine)

    import apowerb.core.run_gate as gate

    async def _plan(owner):
        return None

    monkeypatch.setattr(gate, "resolve_owner_plan", _plan)
    calls = []

    def _bindings(owner, plan):
        async def run_agent(agent_id, message):
            calls.append(("agent", agent_id))
            return "fini"

        async def run_tool(tool, args):
            calls.append(("tool", tool))
            return {"row": args["id"], "score": 0.5}

        async def run_rag(agent_id, query, top_k):  # pragma: no cover
            return {}

        async def run_notify(*a):  # pragma: no cover
            return 1

        return run_agent, run_tool, run_rag, run_notify

    monkeypatch.setattr(rt, "bindings_for", _bindings)

    from apowerb.auth.dependencies import get_current_user
    from apowerb.routers import workflow_defs

    app = FastAPI()
    app.include_router(workflow_defs.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: _user(ALICE)
    c = TestClient(app)
    c.calls = calls
    return c


def _events(r):
    return [json.loads(x[6:]) for x in r.text.split("\n\n") if x.startswith("data: ")]


def _create(client):
    return client.post("/api/workflows/defs", json={"name": "W", "graph": GRAPH}).json()[
        "workflow_id"
    ]


def test_run_until_stops_at_the_node_and_records_it_in_the_run(client):
    wid = _create(client)
    r = client.post(
        f"/api/workflows/defs/{wid}/run", json={"payload": {"id": "B7"}, "until": "t"}
    )

    assert r.status_code == 200
    events = _events(r)
    assert events[-1] == {
        "event": "done",
        "output": {"row": "B7", "score": 0.5},
        "until": "t",
        "partial": True,
    }
    assert client.calls == [("tool", "x.lookup")]
    run = run_main.get_run(events[0]["wid"], owner_id=ALICE)
    assert run["config"]["until"] == "t"


def test_run_until_an_unknown_node_is_a_422_before_any_run(client):
    wid = _create(client)
    r = client.post(f"/api/workflows/defs/{wid}/run", json={"until": "ghost"})

    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "until_unknown_node"
    assert r.json()["detail"]["params"] == {"node": "ghost"}
    assert client.calls == []


async def test_a_partial_run_never_wakes_workflow_done_listeners(monkeypatch, client):
    launched = []

    async def _launch(**kw):  # pragma: no cover - ne doit pas être appelé
        launched.append(kw)
        return "x", None

    monkeypatch.setattr(wt, "launch_triggered_run", _launch)
    src = wm.create_workflow(
        owner_id=ALICE,
        name="Source",
        graph={"version": 1, "nodes": [{"id": "s", "type": "trigger", "config": {"kind": "webhook"}}], "edges": []},
    )
    wm.update_workflow(src["workflow_id"], owner_id=ALICE, expected_version=1, status="published")
    dst = wm.create_workflow(
        owner_id=ALICE,
        name="Listener",
        graph={
            "version": 1,
            "nodes": [
                {
                    "id": "s",
                    "type": "trigger",
                    "config": {"kind": "workflow_done", "workflow_id": src["workflow_id"], "on": "any"},
                }
            ],
            "edges": [],
        },
    )
    wm.update_workflow(dst["workflow_id"], owner_id=ALICE, expected_version=1, status="published")

    def _finish(config):
        run_id = run_main.start_run(trigger="workflow", owner_id=ALICE, config=config)
        return wt.notify_run_finished(
            run_id=run_id, owner_id=ALICE, status="success", output={}, error_message=None
        )

    await _finish({"workflow_id": src["workflow_id"], "version": 1, "payload": {}, "until": "s"})
    assert launched == []
    # Contre-épreuve : le même run, complet, réveille bien l'écouteur.
    await _finish({"workflow_id": src["workflow_id"], "version": 1, "payload": {}})
    assert len(launched) == 1


async def test_replaying_a_partial_run_stays_partial(monkeypatch):
    from apowerb.routers import workflows as wf_router

    seen = {}

    async def _fake_run_graph(graph, **kw):
        seen.update(kw)
        yield 'data: {"event": "done"}\n\n'

    import apowerb.core.run_gate as gate

    async def _plan(owner):
        return None

    monkeypatch.setattr(gate, "resolve_owner_plan", _plan)
    monkeypatch.setattr(wm, "get_workflow_graph_at", lambda *a, **k: GRAPH)
    monkeypatch.setattr(rt, "bindings_for", lambda owner, plan: (None, None, None, None))
    from apowerb.core import workflow_graph

    monkeypatch.setattr(workflow_graph, "run_graph", _fake_run_graph)
    config = {"workflow_id": "w1", "version": 1, "payload": {}, "until": "t"}
    async for _ in wf_router._graph_replay_runner(ALICE, config, None):
        pass
    assert seen["until"] == "t"


async def test_a_replayed_partial_run_keeps_until_and_stays_silent(monkeypatch, client):
    launched = []

    async def _launch(**kw):  # pragma: no cover - ne doit pas être appelé
        launched.append(kw)
        return "x", None

    monkeypatch.setattr(wt, "launch_triggered_run", _launch)
    src = wm.create_workflow(owner_id=ALICE, name="Source", graph=GRAPH)
    listener = wm.create_workflow(
        owner_id=ALICE,
        name="Listener",
        graph={
            "version": 1,
            "nodes": [
                {
                    "id": "s",
                    "type": "trigger",
                    "config": {"kind": "workflow_done", "workflow_id": src["workflow_id"], "on": "any"},
                }
            ],
            "edges": [],
        },
    )
    wm.update_workflow(listener["workflow_id"], owner_id=ALICE, expected_version=1, status="published")
    original = run_main.start_run(
        trigger="workflow",
        owner_id=ALICE,
        config={"workflow_id": src["workflow_id"], "version": 1, "payload": {}, "until": "t"},
    )
    run_main.finish_run(original, status=run_main.STATUS_SUCCESS)

    replay = run_main.prepare_replay(original, owner_id=ALICE, force=True)
    assert replay["config"]["until"] == "t"
    await wt.notify_run_finished(
        run_id=replay["run_id"], owner_id=ALICE, status="success", output={}, error_message=None
    )
    assert launched == []

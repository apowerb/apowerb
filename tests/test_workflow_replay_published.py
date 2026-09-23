"""Rejouer un run de workflow persisté rejoue SON graphe, pas un canvas vide.

Réf. roadmap 94. Un run lancé par ``POST /workflows/defs/{id}/run`` ou par un
trigger ne porte aucun ``agent_ids`` : son entrée, c'est le graphe du workflow
à la version exécutée, plus le payload. Le rejeu relançait le canvas avec une
liste d'agents vide et le consignait ``success`` sans rien exécuter.

Banc SQLite réel (même montage que ``test_agent_runs``) et vrai ``run_graph`` :
ce qui est vérifié, c'est le graphe réellement exécuté et le statut en base.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from apowerb.core import run_gate, run_main, workflow_main
from apowerb.core import workflow_triggers as wt

OWNER = "u@example.com"

GRAPH_V1 = {
    "version": 1,
    "nodes": [
        {"id": "t", "type": "trigger", "config": {"kind": "webhook"}},
        {"id": "o", "type": "output", "config": {"value": "publie:{{t.x}}"}},
    ],
    "edges": [{"source": "t", "target": "o"}],
}
GRAPH_V2 = {
    "version": 1,
    "nodes": [
        {"id": "t", "type": "trigger", "config": {"kind": "webhook"}},
        {"id": "o", "type": "output", "config": {"value": "modifie:{{t.x}}"}},
    ],
    "edges": [{"source": "t", "target": "o"}],
}


def _sqlite_engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def _attach(dbapi_connection, _record):  # pragma: no cover
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS public")

    return engine


@pytest.fixture
def workflows(monkeypatch, tmp_path):
    engine = _sqlite_engine()
    for store in (
        run_main.run_store,
        workflow_main.workflow_store,
        wt.workflow_trigger_store,
    ):
        monkeypatch.setattr(store, "engine", engine)
        store.metadata.create_all(engine)
    monkeypatch.setattr(run_main, "_run_input_dir", lambda run_id: tmp_path / run_id)

    async def _no_plan(owner):
        return None

    monkeypatch.setattr(run_gate, "resolve_owner_plan", _no_plan)

    from apowerb.routers import workflows as module

    module._runs.clear()
    return module


def _user():
    return SimpleNamespace(email=OWNER, user_id=1, role="USER")


async def _drain(response):
    chunks = [chunk async for chunk in response.body_iterator]
    return b"".join(c if isinstance(c, bytes) else c.encode() for c in chunks)


def _published_workflow():
    wf = workflow_main.create_workflow(owner_id=OWNER, name="Tri", graph=GRAPH_V1)
    return workflow_main.update_workflow(
        wf["workflow_id"],
        owner_id=OWNER,
        expected_version=wf["version"],
        status="published",
    )


def _failed_graph_run(wf, trigger="workflow"):
    run_id = run_main.start_run(
        trigger=trigger,
        owner_id=OWNER,
        config={
            "workflow_id": wf["workflow_id"],
            "version": wf["version"],
            "payload": {"x": "bonjour"},
        },
    )
    run_main.finish_run(run_id, status="error", error_message="fournisseur en panne")
    return run_id


def _replay_of(run_id):
    [replay] = [r for r in run_main.list_runs(owner_id=OWNER) if r["replay_of"] == run_id]
    return replay


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "trigger",
    ["workflow", json.dumps({"kind": "webhook", "detail": {}})],
)
async def test_replaying_a_published_run_executes_its_graph(workflows, trigger):
    wf = _published_workflow()
    run_id = _failed_graph_run(wf, trigger=trigger)

    response = await workflows.replay_run(run_id, force=False, current_user=_user())
    body = await _drain(response)

    assert b'"node_start"' in body, "aucun nœud exécuté : le rejeu a tourné à vide"
    assert b"publie:bonjour" in body
    assert _replay_of(run_id)["status"] == "success"


@pytest.mark.asyncio
async def test_a_replay_runs_the_version_the_original_ran_not_a_later_edit(workflows):
    wf = _published_workflow()
    run_id = _failed_graph_run(wf)
    workflow_main.update_workflow(
        wf["workflow_id"],
        owner_id=OWNER,
        expected_version=wf["version"],
        graph=GRAPH_V2,
    )

    body = await _drain(
        await workflows.replay_run(run_id, force=False, current_user=_user())
    )

    assert b"publie:bonjour" in body
    assert b"modifie:" not in body
    assert _replay_of(run_id)["status"] == "success"


@pytest.mark.asyncio
async def test_a_replay_whose_graph_is_gone_fails_and_is_never_a_success(workflows):
    wf = _published_workflow()
    run_id = _failed_graph_run(wf)
    workflow_main.delete_workflow(wf["workflow_id"], owner_id=OWNER)

    body = await _drain(
        await workflows.replay_run(run_id, force=False, current_user=_user())
    )

    assert b'"error"' in body
    replay = _replay_of(run_id)
    assert replay["status"] == "error"
    assert replay["error_message"]

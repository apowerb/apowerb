"""Décision humaine côté serveur : run en pause persisté, liste, reprise.

Banc SQLite réel (même montage que ``test_workflow_replay_published``) et
vrai ``run_graph`` : ce qui est vérifié, c'est le statut en base, la liste
des décisions en attente et le flux réellement rejoué à la reprise.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from apowerb.core import run_gate, run_main, workflow_main
from apowerb.core import workflow_triggers as wt

OWNER = "u@example.com"
ASSIGNEE = "a@example.com"
STRANGER = "z@example.com"


def _graph(assignee=None):
    decision = {
        "question": "Valider {{t.x}} ?",
        "mode": "both",
        "choices": [{"route": "oui", "label": "Oui"}, {"route": "non", "label": "Non"}],
    }
    if assignee:
        decision["assignee"] = assignee
    return {
        "version": 1,
        "nodes": [
            {"id": "t", "type": "trigger", "config": {"kind": "manual"}},
            {"id": "d", "type": "approval", "config": decision},
            {"id": "yes", "type": "output", "config": {"value": "ok:{{d.choice}}"}},
            {"id": "no", "type": "output", "config": {"value": "non:{{d.text}}"}},
        ],
        "edges": [
            {"source": "t", "target": "d"},
            {"source": "d", "target": "yes", "route": "oui"},
            {"source": "d", "target": "no", "route": "non"},
            {"source": "d", "target": "no", "route": "text"},
        ],
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
def api(monkeypatch, tmp_path):
    engine = _sqlite_engine()
    for store in (run_main.run_store, workflow_main.workflow_store, wt.workflow_trigger_store):
        monkeypatch.setattr(store, "engine", engine)
        store.metadata.create_all(engine)
    monkeypatch.setattr(run_main, "_run_input_dir", lambda run_id: tmp_path / run_id)

    async def _no_plan(owner):
        return None

    monkeypatch.setattr(run_gate, "resolve_owner_plan", _no_plan)
    finished = []

    async def _notify(**kwargs):
        finished.append(kwargs)

    monkeypatch.setattr(wt, "notify_run_finished", _notify)
    from apowerb.routers import workflow_defs, workflows

    workflows._runs.clear()
    return SimpleNamespace(defs=workflow_defs, runs=workflows, finished=finished)


def _user(email=OWNER):
    return SimpleNamespace(email=email, user_id=1, role="USER")


async def _drain(response):
    chunks = [chunk async for chunk in response.body_iterator]
    text = b"".join(c if isinstance(c, bytes) else c.encode() for c in chunks).decode()
    return [json.loads(b[len("data: "):]) for b in text.split("\n\n") if b.startswith("data: ")]


async def _paused(api, assignee=None):
    wf = workflow_main.create_workflow(owner_id=OWNER, name="Devis", graph=_graph(assignee))
    events = await _drain(
        await api.defs.run_workflow(
            wf["workflow_id"], api.defs.RunBody(payload={"x": "devis"}), current_user=_user()
        )
    )
    return wf, events


async def _resume(api, run_id, user=OWNER, **answer):
    return await api.runs.resume_run(
        run_id, api.runs.DecisionAnswer(**answer), current_user=_user(user)
    )


@pytest.mark.asyncio
async def test_a_run_that_reaches_a_decision_is_kept_awaiting_and_listed(api):
    wf, events = await _paused(api)
    last = events[-1]
    assert last["event"] == "awaiting_input"
    run_id = events[0]["wid"]
    assert last["run_id"] == run_id and last["question"] == "Valider devis ?"
    assert run_main.get_run(run_id, owner_id=OWNER)["status"] == "awaiting"
    [item] = await api.runs.list_awaiting_runs(current_user=_user())
    assert item["run_id"] == run_id
    assert item["workflow_id"] == wf["workflow_id"] and item["workflow_name"] == "Devis"
    assert item["node_id"] == "d" and item["mode"] == "both"
    assert item["choices"][0] == {"route": "oui", "label": "Oui"}
    assert item["paused_at"]
    assert api.finished == []  # une pause n'est pas une fin (workflow_done)


@pytest.mark.asyncio
async def test_the_saved_state_is_not_exposed_by_the_run_api(api):
    _, events = await _paused(api)
    run = await api.runs.get_run(events[0]["wid"], current_user=_user())
    assert "paused_state" not in run
    assert run["decision"]["question"] == "Valider devis ?"


@pytest.mark.asyncio
async def test_answering_resumes_the_same_run_and_replays_its_stream(api):
    _, events = await _paused(api)
    run_id = events[0]["wid"]
    resumed = await _drain(await _resume(api, run_id, choice="oui"))
    assert resumed[0] == {"event": "run_started", "wid": run_id}
    assert [e["node_id"] for e in resumed if e.get("replayed") and e["event"] == "node_complete"] == ["t"]
    decided = next(e for e in resumed if e["event"] == "node_complete" and e["node_id"] == "d")
    assert decided["output"]["answered_by"] == OWNER
    assert resumed[-1] == {"event": "done", "output": "ok:oui"}
    assert run_main.get_run(run_id, owner_id=OWNER)["status"] == "success"
    assert await api.runs.list_awaiting_runs(current_user=_user()) == []
    assert [f["status"] for f in api.finished] == ["success"]


@pytest.mark.asyncio
async def test_free_text_answer_takes_the_text_route(api):
    _, events = await _paused(api)
    resumed = await _drain(await _resume(api, events[0]["wid"], text="trop cher"))
    assert resumed[-1] == {"event": "done", "output": "non:trop cher"}


@pytest.mark.asyncio
async def test_a_run_already_resumed_cannot_be_answered_again(api):
    _, events = await _paused(api)
    run_id = events[0]["wid"]
    await _drain(await _resume(api, run_id, choice="oui"))
    with pytest.raises(HTTPException) as exc:
        await _resume(api, run_id, choice="non")
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "decision_already_answered"


@pytest.mark.asyncio
async def test_someone_else_cannot_see_or_answer_the_decision(api):
    _, events = await _paused(api)
    run_id = events[0]["wid"]
    assert await api.runs.list_awaiting_runs(current_user=_user(STRANGER)) == []
    with pytest.raises(HTTPException) as exc:
        await _resume(api, run_id, user=STRANGER, choice="oui")
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        await api.runs.abandon_run(run_id, current_user=_user(STRANGER))
    assert exc.value.status_code == 404
    assert run_main.get_run(run_id, owner_id=OWNER)["status"] == "awaiting"


@pytest.mark.asyncio
async def test_the_assignee_sees_and_answers_and_the_run_stays_the_owners(api):
    _, events = await _paused(api, assignee=ASSIGNEE)
    run_id = events[0]["wid"]
    [item] = await api.runs.list_awaiting_runs(current_user=_user(ASSIGNEE))
    assert item["run_id"] == run_id and item["owner_id"] == OWNER
    resumed = await _drain(await _resume(api, run_id, user=ASSIGNEE, choice="non"))
    assert resumed[-1]["event"] == "done"
    assert run_main.get_run(run_id, owner_id=OWNER)["status"] == "success"


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [{"choice": "peut-etre"}, {}, {"text": "   "}])
async def test_an_answer_the_decision_does_not_allow_leaves_it_waiting(api, answer):
    _, events = await _paused(api)
    run_id = events[0]["wid"]
    with pytest.raises(HTTPException) as exc:
        await _resume(api, run_id, **answer)
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "decision_invalid_answer"
    assert run_main.get_run(run_id, owner_id=OWNER)["status"] == "awaiting"


@pytest.mark.asyncio
async def test_abandoning_a_decision_cancels_the_run(api):
    _, events = await _paused(api)
    run_id = events[0]["wid"]
    response = await api.runs.abandon_run(run_id, current_user=_user())
    assert response.status_code == 204
    assert run_main.get_run(run_id, owner_id=OWNER)["status"] == "cancelled"
    assert await api.runs.list_awaiting_runs(current_user=_user()) == []
    with pytest.raises(HTTPException) as exc:
        await _resume(api, run_id, choice="oui")
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_a_second_decision_pauses_the_resumed_run_again(api):
    graph = _graph()
    graph["nodes"].append({"id": "d2", "type": "approval", "config": {"question": "Sûr ?", "mode": "text"}})
    graph["nodes"][2] = {"id": "yes", "type": "output", "config": {"value": "{{d.choice}}/{{d2.text}}"}}
    graph["edges"][1] = {"source": "d", "target": "d2", "route": "oui"}
    graph["edges"].append({"source": "d2", "target": "yes"})
    wf = workflow_main.create_workflow(owner_id=OWNER, name="Devis", graph=graph)
    events = await _drain(
        await api.defs.run_workflow(wf["workflow_id"], api.defs.RunBody(payload={"x": 1}), current_user=_user())
    )
    run_id = events[0]["wid"]
    second = await _drain(await _resume(api, run_id, choice="oui"))
    assert second[-1]["event"] == "awaiting_input" and second[-1]["node_id"] == "d2"
    assert run_main.get_run(run_id, owner_id=OWNER)["status"] == "awaiting"
    third = await _drain(await _resume(api, run_id, text="oui oui"))
    assert third[-1] == {"event": "done", "output": "oui/oui oui"}


@pytest.mark.asyncio
async def test_a_triggered_run_pauses_too_and_waits_in_the_list(api):
    wf = workflow_main.create_workflow(owner_id=OWNER, name="Devis", graph=_graph())
    wf = workflow_main.update_workflow(
        wf["workflow_id"], owner_id=OWNER, expected_version=wf["version"], status="published"
    )
    run_id, task = await wt.launch_triggered_run(
        workflow_id=wf["workflow_id"], owner_id=OWNER, kind="webhook", detail={}, payload={"x": "devis"}
    )
    terminal = await task
    assert terminal["event"] == "awaiting_input" and terminal["run_id"] == run_id
    assert run_main.get_run(run_id, owner_id=OWNER)["status"] == "awaiting"
    [item] = await api.runs.list_awaiting_runs(current_user=_user())
    assert item["run_id"] == run_id and item["question"] == "Valider devis ?"


# --- Robustesse de la reprise (revue du 21/10) --------------------------------


def _age(run_id, hours):
    from datetime import datetime, timedelta

    old = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S.%f")
    t = run_main.run_store.run_table
    with run_main.run_store.engine.begin() as conn:
        conn.execute(t.update().where(t.c.run_id == run_id).values(created_at=old))


@pytest.mark.asyncio
async def test_a_decision_answered_after_hours_is_not_settled_as_an_orphan(api):
    _, events = await _paused(api)
    run_id = events[0]["wid"]
    _age(run_id, 7)
    response = await _resume(api, run_id, choice="oui")
    # Pendant la reprise, un lecteur du statut ne doit pas la clore en error.
    assert run_main.get_run(run_id, owner_id=OWNER)["status"] == "running"
    resumed = await _drain(response)
    assert resumed[-1] == {"event": "done", "output": "ok:oui"}
    assert run_main.get_run(run_id, owner_id=OWNER)["status"] == "success"


@pytest.mark.asyncio
async def test_a_client_gone_before_the_decision_leaves_it_waiting(api):
    _, events = await _paused(api)
    run_id = events[0]["wid"]
    response = await _resume(api, run_id, choice="oui")
    stream = response.body_iterator
    await stream.__anext__()  # run_started, puis le client part
    await stream.aclose()
    assert run_main.get_run(run_id, owner_id=OWNER)["status"] == "awaiting"
    resumed = await _drain(await _resume(api, run_id, choice="non"))
    assert resumed[-1] == {"event": "done", "output": "non:"}


@pytest.mark.asyncio
async def test_a_resume_that_cannot_start_leaves_the_decision_waiting(api, monkeypatch):
    _, events = await _paused(api)
    run_id = events[0]["wid"]

    async def _down(owner):
        raise RuntimeError("plans indisponibles")

    monkeypatch.setattr(run_gate, "resolve_owner_plan", _down)
    resumed = await _drain(await _resume(api, run_id, choice="oui"))
    assert resumed[-1]["event"] == "error"
    assert run_main.get_run(run_id, owner_id=OWNER)["status"] == "awaiting"
    assert [i["run_id"] for i in await api.runs.list_awaiting_runs(current_user=_user())] == [run_id]


@pytest.mark.asyncio
async def test_finishing_never_overwrites_a_pause(api):
    _, events = await _paused(api)
    run_id = events[0]["wid"]
    run_main.finish_run(run_id, status="success")
    assert run_main.get_run(run_id, owner_id=OWNER)["status"] == "awaiting"


@pytest.mark.asyncio
async def test_a_paused_run_is_not_replayed_even_forced(api):
    _, events = await _paused(api)
    with pytest.raises(HTTPException) as exc:
        await api.runs.replay_run(events[0]["wid"], force=True, current_user=_user())
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_the_assignee_does_not_receive_the_owners_data(api):
    _, events = await _paused(api, assignee=ASSIGNEE)
    resumed = await _drain(await _resume(api, events[0]["wid"], user=ASSIGNEE, choice="oui"))
    assert resumed[-1]["event"] == "done"
    assert not [e for e in resumed if "output" in e or "input" in e]
    assert {"event": "node_complete", "node_id": "d"} in [
        {k: e[k] for k in ("event", "node_id")} for e in resumed if e.get("node_id")
    ]


@pytest.mark.asyncio
async def test_only_the_owner_can_abandon(api):
    _, events = await _paused(api, assignee=ASSIGNEE)
    run_id = events[0]["wid"]
    with pytest.raises(HTTPException) as exc:
        await api.runs.abandon_run(run_id, current_user=_user(ASSIGNEE))
    assert exc.value.status_code == 403
    assert run_main.get_run(run_id, owner_id=OWNER)["status"] == "awaiting"


@pytest.mark.asyncio
async def test_an_agent_tool_call_reports_a_waiting_run_not_a_failure(monkeypatch):
    import asyncio

    async def _launch(**kwargs):
        async def _terminal():
            return {"event": "awaiting_input", "run_id": "r9", "node_id": "d", "question": "Q"}

        return "r9", asyncio.ensure_future(_terminal())

    monkeypatch.setattr(wt, "launch_triggered_run", _launch)
    out = await wt.call_agent_tool(
        workflow_id="w1", owner_id=OWNER, tool_name="devis", arguments={}, prior_chain=[]
    )
    assert out == {"status": "awaiting_decision", "run_id": "r9", "question": "Q"}


@pytest.mark.asyncio
async def test_a_partial_test_up_to_the_decision_resumes_as_partial(api):
    wf = workflow_main.create_workflow(owner_id=OWNER, name="Devis", graph=_graph())
    events = await _drain(
        await api.defs.run_workflow(
            wf["workflow_id"],
            api.defs.RunBody(payload={"x": "devis"}, until="d"),
            current_user=_user(),
        )
    )
    assert events[-1]["event"] == "awaiting_input"
    resumed = await _drain(await _resume(api, events[0]["wid"], choice="oui"))
    assert resumed[-1]["partial"] is True and resumed[-1]["until"] == "d"
    assert resumed[-1]["output"]["choice"] == "oui"
    assert not [e for e in resumed if e.get("node_id") in ("yes", "no")]

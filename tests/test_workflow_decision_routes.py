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
    assert item["run_id"] == run_id and item["mine"] is False
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
async def test_a_client_gone_after_answering_does_not_cancel_the_run(api):
    import asyncio

    _, events = await _paused(api)
    run_id = events[0]["wid"]
    response = await _resume(api, run_id, choice="oui")
    stream = response.body_iterator
    await stream.__anext__()  # run_started, puis le client part
    await stream.aclose()
    # La réponse est donnée : la reprise va au bout sans la connexion.
    await asyncio.gather(*list(api.runs._detached_runs))
    run = run_main.get_run(run_id, owner_id=OWNER)
    assert run["status"] == "success"
    with pytest.raises(HTTPException) as exc:
        await _resume(api, run_id, choice="non")
    assert exc.value.status_code == 409


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


# --- Suivi : la décision prise est consignée par le moteur ------------------


def _graph_with_agent_after():
    graph = _graph()
    graph["nodes"][2] = {"id": "yes", "type": "agent", "config": {"agent_id": "agent1"}}
    return graph


@pytest.mark.asyncio
async def test_the_decision_is_recorded_as_taken_before_the_next_step_runs(api, monkeypatch):
    from apowerb.core import workflow_runtime

    seen = []

    async def _agent(agent_id, message):
        t = run_main.run_store.run_table
        with run_main.run_store.engine.begin() as conn:
            row = conn.execute(t.select().where(t.c.status == "running")).fetchone()
        seen.append((row.status, row.paused_state))
        return "fait"

    async def _tool(tool, args):
        return {}

    monkeypatch.setattr(
        workflow_runtime, "bindings_for", lambda owner, plan: (_agent, _tool, None, None)
    )
    wf = workflow_main.create_workflow(owner_id=OWNER, name="Devis", graph=_graph_with_agent_after())
    events = await _drain(
        await api.defs.run_workflow(wf["workflow_id"], api.defs.RunBody(payload={"x": 1}), current_user=_user())
    )
    run_id = events[0]["wid"]
    resumed = await _drain(await _resume(api, run_id, choice="oui"))
    assert resumed[-1]["event"] == "done"
    # Pendant l'étape suivante, la base sait déjà que la décision est prise :
    # une coupure à ce moment ne doit plus remettre le run en attente.
    assert seen == [("running", None)]


@pytest.mark.asyncio
async def test_a_resume_that_died_before_the_decision_goes_back_to_waiting(api):
    _, events = await _paused(api)
    run_id = events[0]["wid"]
    t = run_main.run_store.run_table
    with run_main.run_store.engine.begin() as conn:
        # Le processus est mort juste après la prise : ligne running, état gardé.
        conn.execute(
            t.update().where(t.c.run_id == run_id).values(status="running", resumed_at="2026-01-01 00:00:00.000000")
        )
    # Sans que le propriétaire ouvre le run : la liste et la reprise la voient.
    assert [i["run_id"] for i in await api.runs.list_awaiting_runs(current_user=_user())] == [run_id]
    resumed = await _drain(await _resume(api, run_id, choice="oui"))
    assert resumed[-1] == {"event": "done", "output": "ok:oui"}


@pytest.mark.asyncio
async def test_a_resume_that_died_before_the_decision_is_listed_and_answerable_by_the_assignee(api):
    _, events = await _paused(api, assignee=ASSIGNEE)
    run_id = events[0]["wid"]
    t = run_main.run_store.run_table
    with run_main.run_store.engine.begin() as conn:
        conn.execute(
            t.update().where(t.c.run_id == run_id).values(status="running", resumed_at="2026-01-01 00:00:00.000000")
        )
    assert [i["run_id"] for i in await api.runs.list_awaiting_runs(current_user=_user(ASSIGNEE))] == [run_id]
    resumed = await _drain(await _resume(api, run_id, user=ASSIGNEE, choice="oui"))
    assert resumed[-1]["event"] == "done"


@pytest.mark.asyncio
async def test_a_resume_still_within_its_delay_is_not_put_back_to_waiting(api):
    _, events = await _paused(api)
    run_id = events[0]["wid"]
    t = run_main.run_store.run_table
    with run_main.run_store.engine.begin() as conn:
        conn.execute(t.update().where(t.c.run_id == run_id).values(status="running", resumed_at=run_main._now()))
    assert await api.runs.list_awaiting_runs(current_user=_user()) == []
    with pytest.raises(HTTPException) as exc:
        await _resume(api, run_id, choice="oui")
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_a_resume_that_died_after_the_decision_is_settled_as_an_error(api):
    _, events = await _paused(api)
    run_id = events[0]["wid"]
    t = run_main.run_store.run_table
    with run_main.run_store.engine.begin() as conn:
        conn.execute(
            t.update()
            .where(t.c.run_id == run_id)
            .values(status="running", paused_state=None, resumed_at="2026-01-01 00:00:00.000000")
        )
    assert run_main.get_run(run_id, owner_id=OWNER)["status"] == "error"


# --- Délai d'attente ----------------------------------------------------------


def _expire(run_id):
    """L'échéance de la décision est passée."""
    t = run_main.run_store.run_table
    with run_main.run_store.engine.begin() as conn:
        conn.execute(
            t.update().where(t.c.run_id == run_id).values(expires_at="2026-01-01 00:00:00.000000")
        )


@pytest.mark.asyncio
async def test_the_list_says_until_when_the_decision_waits(api):
    from datetime import datetime, timedelta

    _, events = await _paused(api)
    [item] = await api.runs.list_awaiting_runs(current_user=_user())
    paused = datetime.fromisoformat(item["paused_at"])
    expires = datetime.fromisoformat(item["expires_at"])
    # ISO avec fuseau : le navigateur ne devine pas l'heure du serveur.
    assert paused.tzinfo is not None and expires.tzinfo is not None
    assert expires - paused == timedelta(hours=168)


@pytest.mark.asyncio
async def test_an_expired_decision_cannot_be_answered_even_by_its_owner(api):
    _, events = await _paused(api)
    run_id = events[0]["wid"]
    _expire(run_id)
    with pytest.raises(HTTPException) as exc:
        await _resume(api, run_id, choice="oui")
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "decision_expired"
    run = run_main.get_run(run_id, owner_id=OWNER)
    assert run["status"] == "error" and "expir" in run["error_message"]
    assert await api.runs.list_awaiting_runs(current_user=_user()) == []


@pytest.mark.asyncio
async def test_an_expired_decision_cannot_be_abandoned_either(api):
    _, events = await _paused(api)
    run_id = events[0]["wid"]
    _expire(run_id)
    with pytest.raises(HTTPException) as exc:
        await api.runs.abandon_run(run_id, current_user=_user())
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "decision_expired"


@pytest.mark.asyncio
async def test_an_expired_decision_leaves_the_list_and_is_settled_on_read(api):
    _, events = await _paused(api, assignee=ASSIGNEE)
    run_id = events[0]["wid"]
    _expire(run_id)
    assert await api.runs.list_awaiting_runs(current_user=_user(ASSIGNEE)) == []
    # Ce que lit le planificateur : le run n'attend plus, il peut relancer.
    assert run_main.get_run(run_id, owner_id=OWNER)["status"] == "error"


@pytest.mark.asyncio
async def test_a_decision_still_within_its_delay_is_not_expired(api):
    _, events = await _paused(api)
    run_id = events[0]["wid"]
    # Une autre décision expirée ne touche pas celle-ci.
    _, other = await _paused(api)
    _expire(other[0]["wid"])
    assert [i["run_id"] for i in await api.runs.list_awaiting_runs(current_user=_user())] == [run_id]
    resumed = await _drain(await _resume(api, run_id, choice="oui"))
    assert resumed[-1] == {"event": "done", "output": "ok:oui"}


# --- Instantané au repos ------------------------------------------------------


def _stored_state(run_id):
    t = run_main.run_store.run_table
    with run_main.run_store.engine.begin() as conn:
        return conn.execute(t.select().where(t.c.run_id == run_id)).fetchone()._asdict()["paused_state"]


@pytest.mark.asyncio
async def test_the_saved_state_is_encrypted_at_rest(api):
    _, events = await _paused(api)
    raw = _stored_state(events[0]["wid"])
    assert raw and not raw.startswith("{")
    assert "devis" not in raw and "Valider" not in raw


@pytest.mark.asyncio
async def test_a_state_saved_in_clear_before_encryption_still_resumes(api):
    from apowerb.helpers.encryptor import decrypt_value

    _, events = await _paused(api)
    run_id = events[0]["wid"]
    clear = decrypt_value(_stored_state(run_id))
    t = run_main.run_store.run_table
    with run_main.run_store.engine.begin() as conn:
        conn.execute(t.update().where(t.c.run_id == run_id).values(paused_state=clear))
    resumed = await _drain(await _resume(api, run_id, choice="oui"))
    assert resumed[-1] == {"event": "done", "output": "ok:oui"}


@pytest.mark.asyncio
async def test_the_list_says_whose_run_it_is_without_the_owners_email(api):
    await _paused(api, assignee=ASSIGNEE)
    [mine] = await api.runs.list_awaiting_runs(current_user=_user())
    [theirs] = await api.runs.list_awaiting_runs(current_user=_user(ASSIGNEE))
    assert mine["mine"] is True and theirs["mine"] is False
    assert "owner_id" not in mine and "owner_id" not in theirs
    assert theirs["workflow_name"] == "Devis"


def test_the_assignee_does_not_receive_the_error_detail():
    chunk = 'data: {"event": "error", "code": "x", "detail": "secret du proprio"}\n\n'
    event = json.loads(api_runs()._for_assignee(chunk, ASSIGNEE)[len("data: "):])
    assert event == {"event": "error", "code": "x"}


def test_the_assignee_gets_only_the_structural_params_of_an_error():
    chunk = "data: " + json.dumps({
        "event": "error",
        "code": "agent_asked_question",
        "params": {"node": "yes", "question": "sortie du proprio ?"},
    }) + "\n\n"
    event = json.loads(api_runs()._for_assignee(chunk, ASSIGNEE)[len("data: "):])
    assert event["params"] == {"node": "yes", "question": "…"}


@pytest.mark.asyncio
async def test_a_failure_after_the_assignees_answer_is_told_in_full_to_the_owner(api, monkeypatch):
    """Le flux de l'assigné est expurgé ; l'issue du propriétaire et son alerte ne le sont pas."""
    from apowerb.core import workflow_runtime

    async def _agent(agent_id, message):
        return "Quel est le MARQUEUR-SORTIE ?"

    async def _tool(tool, args):
        return {}

    monkeypatch.setattr(
        workflow_runtime, "bindings_for", lambda owner, plan: (_agent, _tool, None, None)
    )
    graph = _graph(ASSIGNEE)
    graph["nodes"][2] = {
        "id": "yes",
        "type": "agent",
        "config": {"agent_id": "agent1", "on_question": "fail"},
    }
    wf = workflow_main.create_workflow(owner_id=OWNER, name="Devis", graph=graph)
    events = await _drain(
        await api.defs.run_workflow(wf["workflow_id"], api.defs.RunBody(payload={"x": 1}), current_user=_user())
    )
    run_id = events[0]["wid"]

    resumed = await _drain(await _resume(api, run_id, user=ASSIGNEE, choice="oui"))

    assert resumed[-1]["event"] == "error" and resumed[-1]["code"] == "agent_asked_question"
    assert "MARQUEUR-SORTIE" not in json.dumps(resumed)
    run = run_main.get_run(run_id, owner_id=OWNER)
    assert run["status"] == "error"
    assert run["error_message"] == "yes : l'agent a posé une question au lieu de répondre"
    assert run["error"]["failed_node"] == "yes"
    [alert] = [f for f in api.finished if f["status"] == "error"]
    assert alert["error_message"] == run["error_message"]
    assert alert["error_code"] == "agent_asked_question" and alert["error_node"] == "yes"
    t = run_main.run_store.run_table
    with run_main.run_store.engine.begin() as conn:
        row = conn.execute(t.select().where(t.c.run_id == run_id)).fetchone()._asdict()
    assert not [k for k, v in row.items() if isinstance(v, str) and "MARQUEUR-SORTIE" in v]


def api_runs():
    from apowerb.routers import workflows

    return workflows


@pytest.mark.asyncio
async def test_a_decision_taken_by_a_lost_claim_does_not_run_on(api, monkeypatch):
    from apowerb.core import workflow_runtime

    ran = []
    real = workflow_runtime.bindings_for

    def _bindings(owner, plan):
        agent, tool, rag, notify = real(owner, plan)
        # Entre la prise et la décision, la réservation a été perdue (remise
        # en attente puis reprise ailleurs) : la ligne n'est plus la nôtre.
        t = run_main.run_store.run_table
        with run_main.run_store.engine.begin() as conn:
            conn.execute(t.update().values(resumed_at="2026-01-02 00:00:00.000000"))

        async def _agent(agent_id, message):
            ran.append(agent_id)
            return "fait"

        return _agent, tool, rag, notify

    monkeypatch.setattr(workflow_runtime, "bindings_for", _bindings)
    wf = workflow_main.create_workflow(owner_id=OWNER, name="Devis", graph=_graph_with_agent_after())
    events = await _drain(
        await api.defs.run_workflow(wf["workflow_id"], api.defs.RunBody(payload={"x": 1}), current_user=_user())
    )
    run_id = events[0]["wid"]
    resumed = await _drain(await _resume(api, run_id, choice="oui"))
    assert resumed[-1]["event"] == "error"
    assert resumed[-1]["code"] == "decision_already_answered"
    assert ran == []
    t = run_main.run_store.run_table
    with run_main.run_store.engine.begin() as conn:
        row = conn.execute(t.select().where(t.c.run_id == run_id)).fetchone()._asdict()
    # La ligne appartient à l'autre reprise : ni close, ni remise en attente.
    assert row["status"] == "running" and row["paused_state"]


@pytest.mark.asyncio
async def test_the_assignee_is_told_the_decision_expired_not_that_it_is_unknown(api):
    _, events = await _paused(api, assignee=ASSIGNEE)
    run_id = events[0]["wid"]
    _expire(run_id)
    with pytest.raises(HTTPException) as exc:
        await _resume(api, run_id, user=ASSIGNEE, choice="oui")
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "decision_expired"
    # Un tiers ne l'apprend pas pour autant.
    with pytest.raises(HTTPException) as other:
        await _resume(api, run_id, user=STRANGER, choice="oui")
    assert other.value.status_code == 404


@pytest.mark.asyncio
async def test_a_state_sealed_with_another_key_closes_the_run_with_a_coded_refusal(api):
    from cryptography.fernet import Fernet

    _, events = await _paused(api, assignee=ASSIGNEE)
    run_id = events[0]["wid"]
    # ENCRYPT_KEY changée depuis la pause : l'instantané n'est plus lisible.
    foreign = Fernet(Fernet.generate_key()).encrypt(b'{"node_id": "d"}').decode()
    t = run_main.run_store.run_table
    with run_main.run_store.engine.begin() as conn:
        conn.execute(t.update().where(t.c.run_id == run_id).values(paused_state=foreign))
    for _ in range(2):
        with pytest.raises(HTTPException) as exc:
            await _resume(api, run_id, user=ASSIGNEE, choice="oui")
        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "decision_state_unreadable"
    run = run_main.get_run(run_id, owner_id=OWNER)
    assert run["status"] == "error" and "illisible" in run["error_message"]
    assert _stored_state(run_id) is None
    assert await api.runs.list_awaiting_runs(current_user=_user()) == []


@pytest.mark.asyncio
async def test_stopping_a_resumed_run_interrupts_the_node_in_progress(api, monkeypatch):
    import asyncio

    from apowerb.core import workflow_runtime

    started, interrupted = asyncio.Event(), []

    async def _slow_agent(agent_id, message):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            interrupted.append(agent_id)
            raise

    async def _unused(*args, **kwargs):  # pragma: no cover
        raise AssertionError("non appelé")

    monkeypatch.setattr(
        workflow_runtime, "bindings_for", lambda owner, plan: (_slow_agent, _unused, _unused, _unused)
    )
    graph = _graph()
    graph["nodes"][2] = {"id": "yes", "type": "agent", "config": {"agent_id": "agent1"}}
    wf = workflow_main.create_workflow(owner_id=OWNER, name="Lent", graph=graph)
    events = await _drain(
        await api.defs.run_workflow(
            wf["workflow_id"], api.defs.RunBody(payload={"x": "devis"}), current_user=_user()
        )
    )
    run_id = events[0]["wid"]
    response = await _resume(api, run_id, choice="oui")
    await asyncio.wait_for(started.wait(), 5)
    # Arrêter, pas fermer l'onglet : le nœud en cours est interrompu.
    await api.runs.cancel_workflow(run_id, current_user=_user())
    resumed = await asyncio.wait_for(_drain(response), 5)
    await asyncio.gather(*list(api.runs._detached_runs), return_exceptions=True)
    assert interrupted == ["agent1"]
    assert resumed[-1] == {"event": "cancelled"}
    assert run_main.get_run(run_id, owner_id=OWNER)["status"] == "cancelled"


@pytest.mark.asyncio
async def test_only_the_owner_can_stop_a_resumed_run(api):
    _, events = await _paused(api, assignee=ASSIGNEE)
    run_id = events[0]["wid"]
    response = await _resume(api, run_id, user=ASSIGNEE, choice="oui")
    with pytest.raises(HTTPException) as exc:
        await api.runs.cancel_workflow(run_id, current_user=_user(ASSIGNEE))
    assert exc.value.status_code == 403
    resumed = await _drain(response)
    assert resumed[-1]["event"] == "done"
    assert run_main.get_run(run_id, owner_id=OWNER)["status"] == "success"


# --- Déroulé gardé pour l'historique (``/runs/{id}/trace``) -------------------


@pytest.mark.asyncio
async def test_a_resume_by_the_assignee_keeps_the_owners_full_trace(api):
    """Le flux de l'assigné est expurgé ; la trace du propriétaire ne l'est pas."""
    _, events = await _paused(api, assignee=ASSIGNEE)
    run_id = events[0]["wid"]

    resumed = await _drain(await _resume(api, run_id, user=ASSIGNEE, choice="oui"))

    assert not [e for e in resumed if "output" in e]
    trace = run_main.get_trace(run_id, owner_id=OWNER)
    assert trace["events"][-1] == {"event": "done", "output": "ok:oui"}
    decided = next(e for e in trace["events"] if e["event"] == "node_complete" and e["node_id"] == "d")
    assert decided["output"]["answered_by"] == ASSIGNEE


@pytest.mark.asyncio
async def test_a_resume_that_did_not_take_the_decision_keeps_the_pause_trace(api, monkeypatch):
    _, events = await _paused(api)
    run_id = events[0]["wid"]
    paused = run_main.get_trace(run_id, owner_id=OWNER)["events"]
    assert paused[-1]["event"] == "awaiting_input"

    async def _down(owner):
        raise RuntimeError("plans indisponibles")

    monkeypatch.setattr(run_gate, "resolve_owner_plan", _down)
    await _drain(await _resume(api, run_id, choice="oui"))

    assert run_main.get_trace(run_id, owner_id=OWNER)["events"] == paused


@pytest.mark.asyncio
async def test_the_pause_trace_is_encrypted_at_rest(api):
    _, events = await _paused(api)
    t = run_main.run_store.trace_table
    with run_main.run_store.engine.begin() as conn:
        raw = conn.execute(t.select().where(t.c.run_id == events[0]["wid"])).fetchone()._asdict()["events"]
    assert raw and "devis" not in raw and "Valider" not in raw

"""Décision humaine : qui est prévenu, quand, et une seule fois.

Même banc que ``test_workflow_decision_routes`` (SQLite réel, vrai
``run_graph``). Le transport (mail, Teams) est remplacé par un enregistreur :
ce qui est vérifié, c'est qui reçoit quoi, combien de fois, et que les fins
sans réponse passent par ``notify_run_finished``.

S'y ajoutent les points de la revue de la phase 1 : échéance au changement
d'heure, arrêt d'une reprise avant la décision, relais de la reprise détachée
et instantané en clair abîmé.
"""

# Les tests reçoivent la fixture ``api`` importée du banc des routes.
# ruff: noqa: F811

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from apowerb.core import decision_notices, run_gate, run_main, workflow_main
from tests.test_workflow_decision_routes import (  # noqa: F401
    ASSIGNEE,
    OWNER,
    _drain,
    _graph,
    _paused,
    _resume,
    _user,
    api,
)

T0 = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)


class _Outbox:
    def __init__(self):
        self.mails: list[dict] = []
        self.teams: list[dict] = []

    async def mail(self, *, to, subject, html, text):
        self.mails.append({"to": to, "subject": subject, "html": html, "text": text})

    async def teams_post(self, *, to, subject, body):
        self.teams.append({"to": to, "subject": subject, "body": body})

    def to(self, address):
        return [m for m in self.mails if m["to"] == address]


@pytest.fixture
def outbox(monkeypatch):
    box = _Outbox()
    monkeypatch.setattr(decision_notices, "_send_mail", box.mail)
    monkeypatch.setattr(decision_notices, "_send_teams", box.teams_post)
    monkeypatch.setattr(decision_notices, "_app_url", lambda: "https://app.example.com")
    return box


def _row(run_id):
    t = run_main.run_store.run_table
    with run_main.run_store.engine.begin() as conn:
        return conn.execute(t.select().where(t.c.run_id == run_id)).fetchone()._asdict()


def _set(run_id, **values):
    t = run_main.run_store.run_table
    with run_main.run_store.engine.begin() as conn:
        conn.execute(t.update().where(t.c.run_id == run_id).values(**values))


def _deadline(run_id):
    return datetime.fromisoformat(json.loads(_row(run_id)["decision"])["expires_at"])


# --- Prévenir la personne désignée --------------------------------------------


@pytest.mark.asyncio
async def test_the_assignee_is_told_once_with_the_question_and_a_link(api, outbox):
    _, events = await _paused(api, assignee=ASSIGNEE)
    run_id = events[0]["wid"]
    # Le flux qui s'arrête prévient aussitôt ; les passages suivants du
    # planificateur ne renvoient rien.
    await decision_notices.tick()
    await decision_notices.tick()
    [mail] = outbox.mails
    assert mail["to"] == ASSIGNEE
    assert "Valider devis ?" in mail["html"] and "Valider devis ?" in mail["text"]
    assert "Devis" in mail["subject"]
    assert "https://app.example.com/workflows?decision=" + run_id in mail["html"]
    assert [t["to"] for t in outbox.teams] == [ASSIGNEE]


@pytest.mark.asyncio
async def test_without_assignee_the_owner_is_told(api, outbox):
    await _paused(api)
    assert [m["to"] for m in outbox.mails] == [OWNER]


@pytest.mark.asyncio
async def test_the_message_carries_the_question_but_no_other_run_data(
    api, outbox, monkeypatch
):
    graph = _graph(ASSIGNEE)
    graph["nodes"][1]["config"]["question"] = "Valider <b>{{t.x}}</b> ?"
    wf = workflow_main.create_workflow(owner_id=OWNER, name="Devis & co", graph=graph)
    await _drain(
        await api.defs.run_workflow(
            wf["workflow_id"],
            api.defs.RunBody(payload={"x": "devis", "secret": "IBAN-FR76-SECRET"}),
            current_user=_user(),
        )
    )
    [mail] = outbox.mails
    for part in (mail["html"], mail["text"], mail["subject"], outbox.teams[0]["body"]):
        assert "IBAN-FR76-SECRET" not in part
    # La question vient du run : échappée, jamais injectée telle quelle.
    assert "<b>devis</b>" not in mail["html"]
    assert "&lt;b&gt;devis&lt;/b&gt;" in mail["html"]
    assert "Devis &amp; co" in mail["html"]
    # Aucun jeton dans le lien : la page demande la connexion.
    link = mail["html"].split('href="')[1].split('"')[0]
    assert link == f"https://app.example.com/workflows?decision={_last_run()}"


def _last_run():
    t = run_main.run_store.run_table
    with run_main.run_store.engine.begin() as conn:
        return (
            conn.execute(t.select().order_by(t.c.created_at.desc())).fetchone().run_id
        )


@pytest.mark.asyncio
async def test_a_failed_send_is_not_retried_forever_nor_blocks_the_run(
    api, outbox, monkeypatch
):
    tries = []

    async def _down(**kwargs):
        tries.append(kwargs["to"])
        raise RuntimeError("Graph indisponible")

    monkeypatch.setattr(decision_notices, "_send_mail", _down)
    _, events = await _paused(api)
    run_id = events[0]["wid"]
    assert events[-1]["event"] == "awaiting_input"
    assert run_main.get_run(run_id, owner_id=OWNER)["status"] == "awaiting"
    # Au plus une fois : l'avis est réservé avant l'envoi.
    await decision_notices.tick()
    assert tries == [OWNER]
    # Teams part quand même : un canal en panne n'éteint pas l'autre.
    assert [t["to"] for t in outbox.teams] == [OWNER]


@pytest.mark.asyncio
async def test_a_second_decision_of_the_same_run_is_told_again(api, outbox):
    graph = _graph()
    graph["nodes"].append(
        {
            "id": "d2",
            "type": "approval",
            "config": {"question": "Et maintenant ?", "mode": "text"},
        }
    )
    graph["nodes"][2] = {
        "id": "yes",
        "type": "output",
        "config": {"value": "{{d2.text}}"},
    }
    graph["edges"] = [
        {"source": "t", "target": "d"},
        {"source": "d", "target": "d2", "route": "oui"},
        {"source": "d2", "target": "yes"},
        {"source": "d", "target": "no", "route": "non"},
        {"source": "d", "target": "no", "route": "text"},
    ]
    wf = workflow_main.create_workflow(owner_id=OWNER, name="Deux", graph=graph)
    events = await _drain(
        await api.defs.run_workflow(
            wf["workflow_id"],
            api.defs.RunBody(payload={"x": "a"}),
            current_user=_user(),
        )
    )
    run_id = events[0]["wid"]
    resumed = await _drain(await _resume(api, run_id, choice="oui"))
    assert resumed[-1]["event"] == "awaiting_input"
    await decision_notices.tick()
    assert ["Valider" in m["text"] for m in outbox.mails] == [True, False]
    assert "Et maintenant ?" in outbox.mails[1]["text"]


@pytest.mark.asyncio
async def test_a_resume_put_back_to_waiting_is_not_told_again(api, outbox, monkeypatch):
    _, events = await _paused(api)
    run_id = events[0]["wid"]

    async def _down(owner):
        raise RuntimeError("plans indisponibles")

    monkeypatch.setattr(run_gate, "resolve_owner_plan", _down)
    await _drain(await _resume(api, run_id, choice="oui"))
    assert run_main.get_run(run_id, owner_id=OWNER)["status"] == "awaiting"
    await decision_notices.tick()
    assert len(outbox.mails) == 1


# --- Rappel avant l'échéance --------------------------------------------------


@pytest.mark.asyncio
async def test_a_reminder_goes_out_once_a_day_before_the_deadline(api, outbox):
    _, events = await _paused(api, assignee=ASSIGNEE)
    run_id = events[0]["wid"]
    deadline = _deadline(run_id)
    await decision_notices.tick(now=deadline - timedelta(hours=25))
    assert len(outbox.mails) == 1
    await decision_notices.tick(now=deadline - timedelta(hours=23))
    await decision_notices.tick(now=deadline - timedelta(hours=22))
    assert [m["to"] for m in outbox.mails] == [ASSIGNEE, ASSIGNEE]
    assert "Rappel" in outbox.mails[1]["subject"]
    assert "Valider devis ?" in outbox.mails[1]["text"]


@pytest.mark.asyncio
async def test_a_short_delay_is_reminded_half_way(api, outbox):
    graph = _graph()
    graph["nodes"][1]["config"]["expires_after_hours"] = 4
    wf = workflow_main.create_workflow(owner_id=OWNER, name="Court", graph=graph)
    events = await _drain(
        await api.defs.run_workflow(
            wf["workflow_id"],
            api.defs.RunBody(payload={"x": "a"}),
            current_user=_user(),
        )
    )
    run_id = events[0]["wid"]
    deadline = _deadline(run_id)
    await decision_notices.tick(now=deadline - timedelta(hours=2, minutes=1))
    assert len(outbox.mails) == 1
    await decision_notices.tick(now=deadline - timedelta(hours=2))
    assert len(outbox.mails) == 2


@pytest.mark.asyncio
async def test_no_reminder_once_the_decision_is_answered(api, outbox):
    _, events = await _paused(api)
    run_id = events[0]["wid"]
    deadline = _deadline(run_id)
    resumed = await _drain(await _resume(api, run_id, choice="oui"))
    assert resumed[-1]["event"] == "done"
    await decision_notices.tick(now=deadline - timedelta(hours=1))
    assert len(outbox.mails) == 1


@pytest.mark.asyncio
async def test_no_reminder_once_the_decision_is_abandoned(api, outbox):
    _, events = await _paused(api)
    run_id = events[0]["wid"]
    deadline = _deadline(run_id)
    await api.runs.abandon_run(run_id, current_user=_user())
    sent = len(outbox.mails)
    await decision_notices.tick(now=deadline - timedelta(hours=1))
    assert len(outbox.mails) == sent
    assert not any("Rappel" in m["subject"] for m in outbox.mails)


# --- Fins sans réponse --------------------------------------------------------


@pytest.mark.asyncio
async def test_an_expired_decision_tells_the_owner_once_and_ends_the_workflow(
    api, outbox
):
    _, events = await _paused(api, assignee=ASSIGNEE)
    run_id = events[0]["wid"]
    deadline = _deadline(run_id)
    await decision_notices.tick(now=deadline + timedelta(minutes=1))
    await decision_notices.tick(now=deadline + timedelta(minutes=2))
    # Lue entre-temps (liste, reprise) : l'expiration n'est pas annoncée deux fois.
    run_main.get_run(run_id, owner_id=OWNER)
    await decision_notices.tick(now=deadline + timedelta(minutes=3))
    ends = [m for m in outbox.to(OWNER)]
    assert len(ends) == 1 and "expir" in ends[0]["subject"].lower()
    assert [f["run_id"] for f in api.finished] == [run_id]
    [finished] = api.finished
    assert finished["status"] == "error" and finished["owner_id"] == OWNER
    assert "expir" in finished["error_message"]
    assert run_main.get_run(run_id, owner_id=OWNER)["status"] == "error"


@pytest.mark.asyncio
async def test_a_decision_expired_on_read_is_still_announced_by_the_scheduler(
    api, outbox
):
    _, events = await _paused(api)
    run_id = events[0]["wid"]
    _set(run_id, expires_at="2026-01-01 00:00:00.000000")
    # Close à la lecture, avant tout passage du planificateur.
    assert run_main.get_run(run_id, owner_id=OWNER)["status"] == "error"
    await decision_notices.tick()
    await decision_notices.tick()
    assert [f["run_id"] for f in api.finished] == [run_id]
    assert sum("expir" in m["subject"].lower() for m in outbox.mails) == 1


@pytest.mark.asyncio
async def test_an_abandoned_decision_tells_the_owner_and_ends_the_workflow(api, outbox):
    _, events = await _paused(api, assignee=ASSIGNEE)
    run_id = events[0]["wid"]
    await api.runs.abandon_run(run_id, current_user=_user())
    # Annoncé par l'abandon lui-même, sans attendre le planificateur.
    assert [f["run_id"] for f in api.finished] == [run_id]
    assert len(outbox.to(OWNER)) == 1
    await decision_notices.tick()
    ends = outbox.to(OWNER)
    assert len(ends) == 1 and "abandon" in ends[0]["subject"].lower()
    [finished] = api.finished
    assert finished["run_id"] == run_id and finished["status"] == "cancelled"


@pytest.mark.asyncio
async def test_runs_closed_before_this_version_are_not_announced(api, outbox):
    _, events = await _paused(api)
    run_id = events[0]["wid"]
    outbox.mails.clear()
    # Ligne d'avant les notifications : close, sans avis en attente.
    _set(
        run_id,
        status="error",
        error_message=run_main.EXPIRED_MESSAGE,
        paused_state=None,
        end_notice=None,
    )
    await decision_notices.tick()
    assert outbox.mails == [] and api.finished == []


@pytest.mark.asyncio
async def test_an_answered_decision_is_announced_by_the_run_not_twice(api, outbox):
    _, events = await _paused(api)
    run_id = events[0]["wid"]
    await _drain(await _resume(api, run_id, choice="oui"))
    await decision_notices.tick()
    assert [f["run_id"] for f in api.finished] == [run_id]


@pytest.mark.asyncio
async def test_the_scheduler_loop_runs_the_notices(monkeypatch):
    from apowerb.core import flow_scheduler

    calls = []

    async def _tick(now=None):
        calls.append(now)
        raise asyncio.CancelledError  # sortir de la boucle après un passage

    async def _nothing(*args, **kwargs):
        return 0

    monkeypatch.setattr(flow_scheduler, "tick_once", _nothing)
    monkeypatch.setattr(
        flow_scheduler.workflow_mail_subscriptions,
        "reconcile_outlook_subscriptions",
        _nothing,
    )
    monkeypatch.setattr(decision_notices, "tick", _tick)
    with pytest.raises(asyncio.CancelledError):
        await flow_scheduler._loop()
    assert calls == [None]


# --- a. Échéance et changement d'heure ----------------------------------------


@pytest.fixture
def paris():
    """Un serveur à l'heure de Paris : le 25/10/2026, 3 h redevient 2 h."""
    before = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/Paris"
    time.tzset()
    yield
    if before is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = before
    time.tzset()


def _started_run():
    return run_main.start_run(
        trigger="workflow", owner_id=OWNER, config={"workflow_id": "w", "version": 1}
    )


def _expire_at(now):
    with run_main.run_store.engine.begin() as conn:
        run_main._expire_due(conn, now=now)


@pytest.mark.asyncio
async def test_a_deadline_across_the_clock_change_expires_when_it_says(api, paris):
    run_id = _started_run()
    # Pause le 20/10 (heure d'été), échéance le 27/10 (heure d'hiver).
    run_main.pause_run(
        run_id, {"node_id": "d", "prompt": {"question": "?", "mode": "text"}}, now=T0
    )
    deadline = _deadline(run_id)
    assert deadline == T0 + timedelta(hours=168)
    _expire_at(deadline - timedelta(minutes=1))
    assert _row(run_id)["status"] == "awaiting"
    _expire_at(deadline + timedelta(minutes=1))
    assert _row(run_id)["status"] == "error"


@pytest.mark.asyncio
async def test_a_deadline_written_before_in_server_time_still_expires_on_time(
    api, paris
):
    run_id = _started_run()
    run_main.pause_run(
        run_id, {"node_id": "d", "prompt": {"question": "?", "mode": "text"}}
    )
    # Ligne écrite par la version précédente : heure locale du serveur, sans
    # fuseau ; 12:00 à Paris le 27/10 (heure d'hiver) = 11:00 UTC.
    _set(run_id, expires_at="2026-10-27 12:00:00.000000")
    _expire_at(datetime(2026, 10, 27, 10, 59, tzinfo=timezone.utc))
    assert _row(run_id)["status"] == "awaiting"
    _expire_at(datetime(2026, 10, 27, 11, 0, 1, tzinfo=timezone.utc))
    assert _row(run_id)["status"] == "error"


@pytest.mark.asyncio
async def test_a_late_answer_is_told_the_deadline_with_its_offset(api):
    _, events = await _paused(api)
    run_id = events[0]["wid"]
    deadline = _deadline(run_id)
    _expire_at(deadline + timedelta(seconds=1))
    with pytest.raises(HTTPException) as exc:
        await _resume(api, run_id, choice="oui")
    assert exc.value.detail["code"] == "decision_expired"
    assert datetime.fromisoformat(exc.value.detail["expires_at"]) == deadline


# --- b. Arrêter une reprise avant que la décision soit prise -------------------


async def _resume_held_before_the_decision(api, monkeypatch):
    """Une reprise acceptée, bloquée avant que le moteur prenne la réponse."""
    held = asyncio.Event()

    async def _slow_plan(owner):
        held.set()
        await asyncio.Event().wait()

    _, events = await _paused(api)
    run_id = events[0]["wid"]
    monkeypatch.setattr(run_gate, "resolve_owner_plan", _slow_plan)
    response = await _resume(api, run_id, choice="oui")
    stream = response.body_iterator
    await stream.__anext__()  # run_started : la tâche détachée tourne
    await asyncio.wait_for(held.wait(), 5)
    return run_id, stream


@pytest.mark.asyncio
async def test_stopping_a_resume_before_the_decision_closes_the_run(
    api, outbox, monkeypatch
):
    run_id, stream = await _resume_held_before_the_decision(api, monkeypatch)
    await api.runs.cancel_workflow(run_id, current_user=_user())
    rest = [chunk async for chunk in stream]
    await asyncio.gather(*list(api.runs._detached_runs), return_exceptions=True)
    assert json.loads(rest[-1][len(b"data: ") :]) == {"event": "cancelled"}
    # Le flux dit « annulé » : la ligne aussi, elle ne réapparaît pas en attente.
    run = run_main.get_run(run_id, owner_id=OWNER)
    assert run["status"] == "cancelled"
    assert _row(run_id)["paused_state"] is None
    assert await api.runs.list_awaiting_runs(current_user=_user()) == []
    with pytest.raises(HTTPException) as exc:
        await _resume(api, run_id, choice="oui")
    assert exc.value.status_code == 409
    # Annoncé par le flux arrêté lui-même, sans attendre le planificateur.
    assert [f["run_id"] for f in api.finished] == [run_id]
    await decision_notices.tick()
    assert [f["run_id"] for f in api.finished] == [run_id]
    assert api.finished[0]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_a_resume_cut_without_stop_still_waits_for_its_decision(
    api, outbox, monkeypatch
):
    run_id, stream = await _resume_held_before_the_decision(api, monkeypatch)
    # Extinction du serveur, pas un arrêt demandé : la décision attend toujours.
    for task in list(api.runs._detached_runs):
        task.cancel()
    await asyncio.gather(*list(api.runs._detached_runs), return_exceptions=True)
    await stream.aclose()
    assert run_main.get_run(run_id, owner_id=OWNER)["status"] == "awaiting"
    assert api.finished == []


# --- c. Le relais de la reprise détachée --------------------------------------


@pytest.mark.asyncio
async def test_a_detached_resume_stops_queueing_once_the_client_is_gone(api):
    from apowerb.routers import workflows

    go_on = asyncio.Event()

    async def _events():
        yield b"first"
        await go_on.wait()
        for i in range(50):
            yield f"chunk {i}".encode()

    entry: dict = {}
    relay = workflows._detached(_events(), entry)
    assert await relay.__anext__() == b"first"
    await relay.aclose()  # le client part
    go_on.set()
    await asyncio.wait_for(entry["task"], 5)
    # La fin du flux (None) seulement : rien ne s'est empilé pour personne.
    assert entry["queue"].qsize() <= 1


# --- d. Instantané en clair abîmé ---------------------------------------------


@pytest.mark.asyncio
async def test_a_damaged_clear_state_closes_the_run_with_a_coded_refusal(api):
    _, events = await _paused(api, assignee=ASSIGNEE)
    run_id = events[0]["wid"]
    _set(run_id, paused_state='{"node_id": "d", abîmé')
    with pytest.raises(HTTPException) as exc:
        await _resume(api, run_id, user=ASSIGNEE, choice="oui")
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "decision_state_unreadable"
    run = run_main.get_run(run_id, owner_id=OWNER)
    assert (
        run["status"] == "error" and run["error_message"] == run_main.UNREADABLE_MESSAGE
    )


# --- Retrait annoncé à l'assigné ----------------------------------------------


@pytest.mark.asyncio
async def test_an_abandoned_decision_tells_the_assignee_it_is_withdrawn(api, outbox):
    _, events = await _paused(api, assignee=ASSIGNEE)
    run_id = events[0]["wid"]
    await api.runs.abandon_run(run_id, current_user=_user())
    await decision_notices.tick()
    asked, withdrawn = outbox.to(ASSIGNEE)
    assert "Valider devis ?" in asked["text"]
    assert "abandon" in withdrawn["subject"].lower()
    assert "n'attend plus ta décision" in withdrawn["text"]
    # Sa réponse tardive s'entend dire « déjà réglée », pas « inconnue ».
    with pytest.raises(HTTPException) as exc:
        await _resume(api, run_id, user=ASSIGNEE, choice="oui")
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "decision_already_answered"


@pytest.mark.asyncio
async def test_an_expired_decision_tells_the_assignee_once(api, outbox):
    _, events = await _paused(api, assignee=ASSIGNEE)
    run_id = events[0]["wid"]
    deadline = _deadline(run_id)
    await decision_notices.tick(now=deadline + timedelta(minutes=1))
    await decision_notices.tick(now=deadline + timedelta(minutes=2))
    ends = [m for m in outbox.to(ASSIGNEE) if "expir" in m["subject"].lower()]
    assert len(ends) == 1


@pytest.mark.asyncio
async def test_a_stopped_resume_tells_the_assignee_too(api, outbox, monkeypatch):
    held = asyncio.Event()

    async def _slow_plan(owner):
        held.set()
        await asyncio.Event().wait()

    _, events = await _paused(api, assignee=ASSIGNEE)
    run_id = events[0]["wid"]
    monkeypatch.setattr(run_gate, "resolve_owner_plan", _slow_plan)
    stream = (await _resume(api, run_id, user=ASSIGNEE, choice="oui")).body_iterator
    await stream.__anext__()
    await asyncio.wait_for(held.wait(), 5)
    await api.runs.cancel_workflow(run_id, current_user=_user())
    _ = [chunk async for chunk in stream]
    await asyncio.gather(*list(api.runs._detached_runs), return_exceptions=True)
    assert any("abandon" in m["subject"].lower() for m in outbox.to(ASSIGNEE))


@pytest.mark.asyncio
async def test_without_assignee_the_owner_is_not_told_twice_of_the_end(api, outbox):
    _, events = await _paused(api)
    run_id = events[0]["wid"]
    await api.runs.abandon_run(run_id, current_user=_user())
    await decision_notices.tick()
    ends = [m for m in outbox.to(OWNER) if "abandon" in m["subject"].lower()]
    assert len(ends) == 1


# --- Pauses d'avant cette version ---------------------------------------------


@pytest.mark.asyncio
async def test_a_pause_from_before_this_version_is_still_reminded(api, outbox):
    _, events = await _paused(api, assignee=ASSIGNEE)
    run_id = events[0]["wid"]
    deadline = _deadline(run_id)
    # Ligne écrite par la version précédente : pas d'heure de rappel.
    _set(run_id, remind_at=None)
    await decision_notices.tick(now=deadline - timedelta(hours=25))
    assert len(outbox.to(ASSIGNEE)) == 1
    await decision_notices.tick(now=deadline - timedelta(hours=23))
    await decision_notices.tick(now=deadline - timedelta(hours=22))
    assert ["Rappel" in m["subject"] for m in outbox.to(ASSIGNEE)] == [False, True]


@pytest.mark.asyncio
async def test_a_pause_from_before_this_version_with_a_local_deadline_is_reminded(
    api, outbox
):
    _, events = await _paused(api)
    run_id = events[0]["wid"]
    decision = json.loads(_row(run_id)["decision"])
    paused = datetime.fromisoformat(decision["paused_at"])
    legacy_deadline = (paused + timedelta(hours=168)).astimezone()
    _set(
        run_id,
        remind_at=None,
        expires_at=legacy_deadline.strftime("%Y-%m-%d %H:%M:%S.%f"),
    )
    await decision_notices.tick(now=legacy_deadline - timedelta(hours=23))
    assert ["Rappel" in m["subject"] for m in outbox.mails] == [False, True]


@pytest.mark.asyncio
async def test_a_pause_with_an_unreadable_deadline_is_skipped_not_fatal(api, outbox):
    _, events = await _paused(api)
    run_id = events[0]["wid"]
    _, other = await _paused(api)
    _set(run_id, remind_at=None, expires_at="pas une date")
    # Les autres décisions passent toujours ; la ligne abîmée reste en attente.
    assert await decision_notices.tick() >= 0
    assert run_main.get_run(other[0]["wid"], owner_id=OWNER)["status"] == "awaiting"
    assert _row(run_id)["status"] == "awaiting"


# --- Plusieurs passages en même temps -----------------------------------------


@pytest.mark.asyncio
async def test_concurrent_passes_send_each_notice_once(api, outbox, monkeypatch):
    _, events = await _paused(api, assignee=ASSIGNEE)
    run_id = events[0]["wid"]
    outbox.mails.clear()
    _set(run_id, notified_at=None)
    # Deux réplicas qui passent au même moment, avant et après l'échéance du
    # rappel puis après l'échéance tout court.
    deadline = _deadline(run_id)
    for moment in (
        None,
        deadline - timedelta(hours=1),
        deadline + timedelta(minutes=1),
    ):
        await asyncio.gather(*(decision_notices.tick(now=moment) for _ in range(5)))
    subjects = [m["subject"] for m in outbox.to(ASSIGNEE)]
    assert len(subjects) == 3, subjects  # avis, rappel, expiration
    assert len(outbox.to(OWNER)) == 1  # expiration
    assert [f["run_id"] for f in api.finished] == [run_id]


def test_a_reservation_is_won_by_one_connection_only(tmp_path, monkeypatch):
    """Le vrai garde : un ``UPDATE ... WHERE ... IS NULL`` sur une base fichier,
    disputé par plusieurs connexions dans des threads (comme des réplicas)."""
    from concurrent.futures import ThreadPoolExecutor

    from sqlalchemy import create_engine, event

    engine = create_engine(
        f"sqlite:///{tmp_path / 'runs.db'}", connect_args={"timeout": 30}
    )

    @event.listens_for(engine, "connect")
    def _attach(dbapi_connection, _record):  # pragma: no cover
        dbapi_connection.execute(
            f"ATTACH DATABASE '{tmp_path / 'public.db'}' AS public"
        )

    monkeypatch.setattr(run_main.run_store, "engine", engine)
    run_main.run_store.metadata.create_all(engine)
    run_id = run_main.start_run(trigger="workflow", owner_id=OWNER, config={})
    t = run_main.run_store.run_table
    stamp = datetime.now(timezone.utc).isoformat()

    def _try(_):
        return decision_notices._reserve(
            run_id, t.c.notified_at.is_(None), notified_at=stamp
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        won = list(pool.map(_try, range(16)))
    assert won.count(True) == 1


# --- Transport ----------------------------------------------------------------

_REAL_SEND_MAIL = decision_notices._send_mail
_REAL_SEND_TEAMS = decision_notices._send_teams


class _Response:
    def __init__(self, status):
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Client:
    posts: list = []
    status = 200

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        _Client.posts.append({"url": url, "json": json, "kwargs": self.kwargs})
        return _Response(_Client.status)


@pytest.fixture
def teams_http(monkeypatch):
    import httpx

    _Client.posts, _Client.status = [], 200
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return _Client


@pytest.mark.asyncio
async def test_the_mail_goes_through_the_system_mailer(monkeypatch):
    from apowerb.helpers import system_mailer

    sent = []

    async def _mailer(**kwargs):
        sent.append(kwargs)
        return True

    monkeypatch.setattr(system_mailer, "send_system_email", _mailer)
    await _REAL_SEND_MAIL(to="a@example.com", subject="S", html="<p>H</p>", text="T")
    assert sent == [
        {"to": "a@example.com", "subject": "S", "html": "<p>H</p>", "text": "T"}
    ]


@pytest.mark.asyncio
async def test_teams_posts_a_card_to_the_recipients_webhook(monkeypatch, teams_http):
    from apowerb.integrations import teams

    url = "https://contoso.webhook.office.com/webhookb2/x"

    async def _url(email):
        assert email == ASSIGNEE
        return url

    monkeypatch.setattr(teams, "get_teams_webhook_url_for_owner", _url)
    monkeypatch.setattr(teams, "validate_teams_webhook_url", lambda u: u)
    await _REAL_SEND_TEAMS(
        to=ASSIGNEE, subject="Décision attendue", body="Valider ?\nlien"
    )
    [post] = teams_http.posts
    assert post["url"] == url
    assert post["kwargs"]["follow_redirects"] is False
    texts = [b["text"] for b in post["json"]["attachments"][0]["content"]["body"]]
    assert texts == ["Décision attendue", "Valider ?\nlien"]


@pytest.mark.asyncio
async def test_teams_is_skipped_for_someone_without_account_or_webhook(
    monkeypatch, teams_http
):
    from apowerb.integrations import teams
    from apowerb.users.exceptions import UserNotFoundException

    async def _unknown(email):
        raise UserNotFoundException("User not found")

    monkeypatch.setattr(teams, "get_teams_webhook_url_for_owner", _unknown)
    await _REAL_SEND_TEAMS(to="externe@example.com", subject="S", body="B")

    async def _none(email):
        return None

    monkeypatch.setattr(teams, "get_teams_webhook_url_for_owner", _none)
    await _REAL_SEND_TEAMS(to=ASSIGNEE, subject="S", body="B")
    assert teams_http.posts == []


@pytest.mark.asyncio
async def test_a_refused_or_failing_webhook_raises_for_the_caller_to_log(
    monkeypatch, teams_http
):
    from apowerb.integrations import teams

    async def _url(email):
        return "https://evil.example.com/hook"

    monkeypatch.setattr(teams, "get_teams_webhook_url_for_owner", _url)
    with pytest.raises(teams.TeamsWebhookRefused):
        await _REAL_SEND_TEAMS(to=ASSIGNEE, subject="S", body="B")
    assert teams_http.posts == []

    monkeypatch.setattr(teams, "validate_teams_webhook_url", lambda u: u)
    teams_http.status = 500
    with pytest.raises(RuntimeError):
        await _REAL_SEND_TEAMS(to=ASSIGNEE, subject="S", body="B")


@pytest.mark.asyncio
async def test_a_teams_failure_does_not_stop_the_mail(api, outbox, monkeypatch):
    async def _down(**kwargs):
        raise RuntimeError("webhook en panne")

    monkeypatch.setattr(decision_notices, "_send_teams", _down)
    await _paused(api, assignee=ASSIGNEE)
    assert [m["to"] for m in outbox.mails] == [ASSIGNEE]

"""Alerte d'échec : « Me prévenir en cas d'échec » (réglage du workflow).

Active par défaut sur un workflow publié, coupée par
``settings.notify_on_failure: false``, allumée sur un brouillon par ``true``.
Une seule alerte par run (réservation en base, valable entre réplicas) ; au
plus une par workflow et par heure, la suivante dit combien d'échecs ont été
regroupés. Ni « Tester jusqu'ici » (``config.until``), ni run annulé, ni la
fin d'une décision humaine, que ``decision_notices`` annonce déjà.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from apowerb.core import run_main
from apowerb.core import workflow_alerts as wa
from apowerb.core import workflow_main as wm
from apowerb.core import workflow_triggers as wt
from apowerb.core.workflow_graph import GraphError

ALICE = "alice@acme.fr"


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
    monkeypatch.setattr(run_main.run_store, "engine", engine)
    run_main.run_store.metadata.create_all(engine)
    return engine


class _Outbox:
    def __init__(self):
        self.mails = []
        self.teams = []
        self.teams_error = None

    async def mail(self, to, subject, body):
        self.mails.append({"to": to, "subject": subject, "body": body})

    async def team(self, owner, subject, body):
        if self.teams_error is not None:
            raise self.teams_error
        self.teams.append({"owner": owner, "subject": subject, "body": body})


@pytest.fixture()
def outbox(monkeypatch):
    box = _Outbox()
    monkeypatch.setattr(wa, "_send_mail", box.mail)
    monkeypatch.setattr(wa, "_send_teams", box.team)
    monkeypatch.setattr(wa, "_app_url", lambda: "https://app.example.com")

    # Les écouteurs workflow_done ne sont pas l'objet de ce fichier.
    async def _no_listeners(**_kw):
        return []

    monkeypatch.setattr(wt, "fire_workflow_done_triggers", _no_listeners)
    return box


def _workflow(*, published=True, notify=None, name="Relances"):
    graph = {"version": 1, "nodes": [{"id": "t", "type": "trigger"}], "edges": []}
    if notify is not None:
        graph["settings"] = {"notify_on_failure": notify}
    wf = wm.create_workflow(owner_id=ALICE, name=name, graph=graph)
    if published:
        wm.update_workflow(
            wf["workflow_id"],
            owner_id=ALICE,
            expected_version=wf["version"],
            status="published",
        )
    return wf["workflow_id"]


def _failed_run(workflow_id, *, status="error", until=None, message="ToolError: boom"):
    config = {"workflow_id": workflow_id}
    if until:
        config["until"] = until
    run_id = run_main.start_run(trigger="workflow", owner_id=ALICE, config=config)
    run_main.finish_run(
        run_id, status=status, error_message=message if status == "error" else None
    )
    return run_id


async def _finish(run_id, status="error", message="ToolError: boom", code="tool_down"):
    await wt.notify_run_finished(
        run_id=run_id,
        owner_id=ALICE,
        status=status,
        output=None,
        error_message=message,
        error_code=code if status == "error" else None,
        error_node="a" if status == "error" else None,
    )


@pytest.fixture()
def clock(monkeypatch):
    now = [datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc)]
    monkeypatch.setattr(wa, "_clock", lambda: now[0])
    return now


async def test_a_failed_published_run_alerts_its_owner_once(outbox, clock):
    wid = _workflow()
    run_id = _failed_run(wid)
    await _finish(run_id)
    # Même run signalé deux fois, hors du délai par workflow : c'est la
    # réservation par run, et elle seule, qui doit l'arrêter.
    clock[0] += timedelta(hours=2)
    await _finish(run_id)
    assert len(outbox.mails) == 1
    mail = outbox.mails[0]
    assert mail["to"] == ALICE
    assert "Relances" in mail["subject"]
    assert run_id in mail["body"]
    assert "tool_down" in mail["body"] and "étape a" in mail["body"]
    assert f"https://app.example.com/workflows/{wid}" in mail["body"]
    # Le message d'erreur peut porter des données du run : jamais dans l'alerte.
    assert "boom" not in mail["body"] and "boom" not in mail["subject"]


async def test_the_per_run_claim_is_kept_in_the_database(outbox):
    wid = _workflow()
    run_id = _failed_run(wid)
    await _finish(run_id)
    t = run_main.run_store.run_table
    with run_main.run_store.engine.begin() as conn:
        row = conn.execute(t.select().where(t.c.run_id == run_id)).fetchone()
    assert row._mapping["failure_alert"] == wa.SENT
    assert (
        await wa.alert_run_failed(
            run_id=run_id, owner_id=ALICE, workflow_id=wid, error_code="x"
        )
        is None
    )


async def test_draft_is_silent_by_default_but_can_opt_in(outbox):
    await _finish(_failed_run(_workflow(published=False)))
    assert outbox.mails == []
    await _finish(_failed_run(_workflow(published=False, notify=True)))
    assert len(outbox.mails) == 1


async def test_published_workflow_can_opt_out(outbox):
    await _finish(_failed_run(_workflow(notify=False)))
    assert outbox.mails == []


@pytest.mark.parametrize("status", ["success", "cancelled"])
async def test_only_errors_alert(outbox, status):
    wid = _workflow()
    await _finish(_failed_run(wid, status=status), status=status, message=None)
    assert outbox.mails == []


async def test_test_up_to_here_never_alerts(outbox):
    wid = _workflow()
    await _finish(_failed_run(wid, until="a"))
    assert outbox.mails == []


async def test_a_failing_loop_is_grouped_then_counted(outbox, clock):
    wid = _workflow()
    for _ in range(3):
        await _finish(_failed_run(wid))
        clock[0] += timedelta(minutes=5)
    assert len(outbox.mails) == 1
    clock[0] = datetime(2026, 10, 22, 10, 1)
    await _finish(_failed_run(wid))
    assert len(outbox.mails) == 2
    assert "2 autre" in outbox.mails[1]["body"]
    # Le compteur repart de zéro après une alerte envoyée.
    clock[0] = datetime(2026, 10, 22, 11, 2)
    await _finish(_failed_run(wid))
    assert "autre" not in outbox.mails[2]["body"]


async def test_cooldown_is_per_workflow(outbox):
    await _finish(_failed_run(_workflow(name="A")))
    await _finish(_failed_run(_workflow(name="B")))
    assert len(outbox.mails) == 2


async def test_teams_is_used_when_configured(outbox):
    await _finish(_failed_run(_workflow()))
    assert len(outbox.teams) == 1 and len(outbox.mails) == 1


async def test_missing_teams_integration_is_not_an_error(outbox):
    outbox.teams_error = GraphError("pas de Teams", code="teams_not_configured")
    await _finish(_failed_run(_workflow()))
    assert len(outbox.mails) == 1


async def test_a_broken_mail_server_does_not_break_the_run_end(outbox, monkeypatch):
    calls = []

    async def _boom(*_a):
        raise OSError("smtp down")

    async def _listeners(**kw):
        calls.append(kw)
        return []

    monkeypatch.setattr(wa, "_send_mail", _boom)
    monkeypatch.setattr(wt, "fire_workflow_done_triggers", _listeners)
    await _finish(_failed_run(_workflow()))
    assert len(calls) == 1


# --- fin d'une décision humaine -----------------------------------------------
# ``decision_notices`` annonce déjà au propriétaire une décision expirée,
# abandonnée ou illisible, puis passe par ``notify_run_finished`` : l'alerte
# d'échec s'efface devant cette annonce, sans priver les écouteurs de la fin.


class _DecisionOutbox:
    def __init__(self):
        self.mails = []

    async def mail(self, *, to, subject, html, text):
        self.mails.append({"to": to, "subject": subject})

    async def teams(self, *, to, subject, body):
        return None


@pytest.fixture()
def decision_outbox(monkeypatch):
    from apowerb.core import decision_notices as dn

    box = _DecisionOutbox()
    monkeypatch.setattr(dn, "_send_mail", box.mail)
    monkeypatch.setattr(dn, "_send_teams", box.teams)
    monkeypatch.setattr(dn, "_app_url", lambda: "https://app.example.com")
    return box


def _expired_run(workflow_id):
    run_id = run_main.start_run(
        trigger="workflow", owner_id=ALICE, config={"workflow_id": workflow_id}
    )
    t = run_main.run_store.run_table
    with run_main.run_store.engine.begin() as conn:
        conn.execute(
            t.update()
            .where(t.c.run_id == run_id)
            .values(
                status=run_main.STATUS_AWAITING,
                expires_at="2026-01-01 00:00:00.000000",
                notified_at="2025-12-31T00:00:00+00:00",
                reminded_at="2025-12-31T12:00:00+00:00",
            )
        )
    return run_id


async def test_an_expired_decision_is_announced_once_without_a_second_alert(
    outbox, decision_outbox, monkeypatch
):
    from apowerb.core import decision_notices as dn

    ends = []

    async def _listeners(**kw):
        ends.append(kw)
        return []

    monkeypatch.setattr(wt, "fire_workflow_done_triggers", _listeners)
    run_id = _expired_run(_workflow())
    await dn.tick()
    await dn.tick()
    assert [m["subject"] for m in decision_outbox.mails] == [
        "Décision expirée — Relances"
    ]
    assert outbox.mails == [] and outbox.teams == []
    assert (
        len(ends) == 1
    )  # la fin passe quand même aux déclencheurs « End of a workflow »
    assert run_main.get_run(run_id, ALICE)["status"] == run_main.STATUS_ERROR


async def test_a_decision_end_never_alerts_even_when_finished_again(outbox):
    # Contre-exemple : même fin, même message, mais annoncée par la décision.
    run_id = _failed_run(_workflow(), message=run_main.EXPIRED_MESSAGE)
    t = run_main.run_store.run_table
    with run_main.run_store.engine.begin() as conn:
        conn.execute(t.update().where(t.c.run_id == run_id).values(end_notice="sent"))
    await _finish(run_id, message=run_main.EXPIRED_MESSAGE)
    assert outbox.mails == []
    # Le même échec sans décision, lui, alerte.
    await _finish(
        _failed_run(_workflow(name="Autre")), message=run_main.EXPIRED_MESSAGE
    )
    assert len(outbox.mails) == 1


# --- ordre et horloge ------------------------------------------------------------


async def test_listeners_never_wait_for_the_alert(outbox, monkeypatch):
    import asyncio

    heard = asyncio.Event()

    async def _slow_alert(**_kw):
        # Ne finit que si les écouteurs ont déjà été prévenus.
        await heard.wait()

    async def _listeners(**_kw):
        heard.set()
        return []

    monkeypatch.setattr(wa, "alert_run_failed", _slow_alert)
    monkeypatch.setattr(wt, "fire_workflow_done_triggers", _listeners)
    await asyncio.wait_for(_finish(_failed_run(_workflow())), timeout=2)
    assert heard.is_set()


async def test_a_broken_alert_does_not_break_the_listeners(outbox, monkeypatch):
    calls = []

    async def _boom(**_kw):
        raise RuntimeError("alerte en panne")

    async def _listeners(**kw):
        calls.append(kw)
        return []

    monkeypatch.setattr(wa, "alert_run_failed", _boom)
    monkeypatch.setattr(wt, "fire_workflow_done_triggers", _listeners)
    await _finish(_failed_run(_workflow()))
    assert len(calls) == 1


def _alert_state(run_id):
    t = run_main.run_store.run_table
    with run_main.run_store.engine.begin() as conn:
        row = conn.execute(t.select().where(t.c.run_id == run_id)).fetchone()
    return row._mapping["failure_alert"]


async def test_a_cancelled_caller_still_sends_the_alert(outbox, monkeypatch):
    # Client parti pendant l'envoi : l'alerte part quand même, sinon le run
    # resterait « claimed » et l'heure prise sans aucun mail.
    import asyncio

    started, release = asyncio.Event(), asyncio.Event()
    sent = outbox.mail

    async def _slow_mail(*args):
        started.set()
        await release.wait()
        await sent(*args)

    monkeypatch.setattr(wa, "_send_mail", _slow_mail)
    run_id = _failed_run(_workflow())
    caller = asyncio.create_task(_finish(run_id))
    await started.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    release.set()
    for _ in range(50):
        if _alert_state(run_id) == wa.SENT:
            break
        await asyncio.sleep(0.01)
    assert len(outbox.mails) == 1
    assert _alert_state(run_id) == wa.SENT


async def test_a_cancelled_send_gives_back_the_run_and_the_hour(outbox, monkeypatch):
    # Arrêt du serveur pendant l'envoi : rien n'est parti, la réservation et
    # le créneau sont rendus — l'échec suivant alerte au lieu d'être regroupé.
    import asyncio

    started = asyncio.Event()

    async def _stuck_mail(*_args):
        started.set()
        await asyncio.Event().wait()

    wid = _workflow()
    first = _failed_run(wid)
    monkeypatch.setattr(wa, "_send_mail", _stuck_mail)
    task = asyncio.create_task(
        wa.alert_run_failed(run_id=first, owner_id=ALICE, workflow_id=wid)
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _alert_state(first) is None
    monkeypatch.setattr(wa, "_send_mail", outbox.mail)
    await _finish(_failed_run(wid))
    assert len(outbox.mails) == 1
    assert "regroupés" not in outbox.mails[0]["body"]


def test_the_alert_clock_is_utc():
    assert wa._clock().utcoffset() == timedelta(0)


# --- « Reprendre ici » -------------------------------------------------------------
# Une reprise est un nouveau run (``replay_of``) qui finit par le même
# ``notify_run_finished`` : réussie, elle n'alerte pas ; en échec, elle suit
# la règle commune (une alerte par run, une par heure et par workflow).


def _retry_of(run_id, workflow_id, *, status):
    retry = run_main.start_run(
        trigger="workflow",
        owner_id=ALICE,
        config={"workflow_id": workflow_id, "retry_from": "a"},
        replay_of=run_id,
    )
    run_main.finish_run(
        retry, status=status, error_message="x" if status == "error" else None
    )
    return retry


async def test_a_successful_retry_does_not_alert_again(outbox):
    wid = _workflow()
    failed = _failed_run(wid)
    await _finish(failed)
    retry = _retry_of(failed, wid, status="success")
    await _finish(retry, status="success")
    assert len(outbox.mails) == 1
    assert retry not in outbox.mails[0]["body"]


async def test_a_failed_retry_alerts_once_as_its_own_run(outbox, clock):
    wid = _workflow()
    failed = _failed_run(wid)
    await _finish(failed)
    clock[0] += timedelta(hours=2)  # hors du délai par workflow
    retry = _retry_of(failed, wid, status="error")
    await _finish(retry)
    await _finish(retry)
    await _finish(failed)
    assert [retry in m["body"] for m in outbox.mails] == [False, True]


async def test_a_failed_retry_within_the_hour_is_grouped(outbox):
    wid = _workflow()
    failed = _failed_run(wid)
    await _finish(failed)
    await _finish(_retry_of(failed, wid, status="error"))
    assert len(outbox.mails) == 1

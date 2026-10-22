"""Alerte d'échec : « Me prévenir en cas d'échec » (réglage du workflow).

Active par défaut sur un workflow publié, coupée par
``settings.notify_on_failure: false``, allumée sur un brouillon par ``true``.
Une seule alerte par run (réservation en base, valable entre réplicas) ; au
plus une par workflow et par heure, la suivante dit combien d'échecs ont été
regroupés. Ni « Tester jusqu'ici » (``config.until``), ni run annulé. Une
décision humaine expirée alerte aussi, depuis la boucle du planificateur.
"""

from datetime import datetime, timedelta

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
            wf["workflow_id"], owner_id=ALICE, expected_version=wf["version"], status="published"
        )
    return wf["workflow_id"]


def _failed_run(workflow_id, *, status="error", until=None, message="ToolError: boom"):
    config = {"workflow_id": workflow_id}
    if until:
        config["until"] = until
    run_id = run_main.start_run(trigger="workflow", owner_id=ALICE, config=config)
    run_main.finish_run(run_id, status=status, error_message=message if status == "error" else None)
    return run_id


async def _finish(run_id, status="error", message="ToolError: boom"):
    await wt.notify_run_finished(
        run_id=run_id, owner_id=ALICE, status=status, output=None, error_message=message
    )


@pytest.fixture()
def clock(monkeypatch):
    now = [datetime(2026, 10, 22, 9, 0)]
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
    assert run_id in mail["body"] and "ToolError: boom" in mail["body"]


async def test_the_per_run_claim_is_kept_in_the_database(outbox):
    wid = _workflow()
    run_id = _failed_run(wid)
    await _finish(run_id)
    t = run_main.run_store.run_table
    with run_main.run_store.engine.begin() as conn:
        row = conn.execute(t.select().where(t.c.run_id == run_id)).fetchone()
    assert row._mapping["failure_alert"] == wa.SENT
    assert await wa.alert_run_failed(
        run_id=run_id, owner_id=ALICE, workflow_id=wid, error_message="x"
    ) is None


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


# --- décision humaine expirée ------------------------------------------------


def _expired_run(workflow_id, *, expired_since=timedelta(minutes=1)):
    run_id = run_main.start_run(
        trigger="workflow", owner_id=ALICE, config={"workflow_id": workflow_id}
    )
    stamp = (datetime.now() - expired_since).strftime("%Y-%m-%d %H:%M:%S.%f")
    t = run_main.run_store.run_table
    with run_main.run_store.engine.begin() as conn:
        conn.execute(
            t.update()
            .where(t.c.run_id == run_id)
            .values(status=run_main.STATUS_AWAITING, expires_at=stamp)
        )
    return run_id


async def test_an_expired_decision_alerts_once(outbox, monkeypatch):
    run_id = _expired_run(_workflow())
    await wa.alert_expired_decisions()
    monkeypatch.setattr(wa, "ALERT_COOLDOWN", timedelta(0))
    await wa.alert_expired_decisions()
    assert len(outbox.mails) == 1
    assert run_id in outbox.mails[0]["body"]
    assert run_main.EXPIRED_MESSAGE in outbox.mails[0]["body"]


async def test_an_old_expiry_found_late_is_not_alerted(outbox):
    # Déploiement : les décisions closes depuis longtemps ne réveillent personne.
    run_id = _expired_run(_workflow())
    await wa.alert_expired_decisions()
    outbox.mails.clear()
    t = run_main.run_store.run_table
    old = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S.%f")
    with run_main.run_store.engine.begin() as conn:
        conn.execute(
            t.update().where(t.c.run_id == run_id).values(failure_alert=None, finished_at=old)
        )
    await wa.alert_expired_decisions()
    assert outbox.mails == []


async def test_a_waiting_decision_is_not_alerted(outbox):
    _expired_run(_workflow(), expired_since=timedelta(hours=-5))  # échéance future
    await wa.alert_expired_decisions()
    assert outbox.mails == []

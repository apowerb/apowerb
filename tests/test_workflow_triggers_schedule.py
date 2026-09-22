"""Calcul de ``next_run_at`` et tick du planificateur (``core.flow_scheduler``).

``asyncio_mode = "auto"`` (pyproject.toml) : les ``async def test_...``
tournent directement, pas besoin de ``@pytest.mark.asyncio``.
"""

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from apowerb.core import flow_scheduler
from apowerb.core import workflow_main as wm
from apowerb.core import workflow_triggers as wt

ALICE = "alice@acme.fr"


def _schedule_graph(cron=None, at=None, timezone_name="Europe/Paris"):
    cfg = {"kind": "schedule", "timezone": timezone_name}
    if cron:
        cfg["cron"] = cron
    if at:
        cfg["at"] = at
    return {
        "version": 1,
        "nodes": [{"id": "start", "type": "trigger", "config": cfg}],
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


def _row(workflow_id):
    t = wt.workflow_trigger_store.trigger_table
    with wt.workflow_trigger_store.engine.begin() as conn:
        row = conn.execute(t.select().where(t.c.workflow_id == workflow_id)).fetchone()
    return dict(row._mapping) if row is not None else None


def _set_row(workflow_id, **values):
    t = wt.workflow_trigger_store.trigger_table
    with wt.workflow_trigger_store.engine.begin() as conn:
        conn.execute(t.update().where(t.c.workflow_id == workflow_id).values(**values))


# --- compute_next_run --------------------------------------------------------


def test_compute_next_run_hourly_cron():
    after = datetime(2026, 1, 1, 10, 15, tzinfo=timezone.utc)
    nxt = wt.compute_next_run({"cron": "0 * * * *", "timezone": "UTC"}, after=after)
    assert nxt == datetime(2026, 1, 1, 11, 0, tzinfo=timezone.utc)


def test_compute_next_run_daily_cron_with_timezone():
    # 9h Europe/Paris (hiver, UTC+1) le 2 janvier 2026, calculé depuis le 1er
    # à 23h UTC (0h locale) -> prochaine échéance : 2 janvier 8h UTC.
    after = datetime(2026, 1, 1, 23, 0, tzinfo=timezone.utc)
    nxt = wt.compute_next_run(
        {"cron": "0 9 * * *", "timezone": "Europe/Paris"}, after=after
    )
    assert nxt == datetime(2026, 1, 2, 8, 0, tzinfo=timezone.utc)


def test_compute_next_run_at_in_the_future_returns_it():
    after = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    nxt = wt.compute_next_run({"at": "2026-06-01T10:00:00+00:00"}, after=after)
    assert nxt == datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)


def test_compute_next_run_at_in_the_past_returns_none():
    after = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    nxt = wt.compute_next_run({"at": "2020-01-01T00:00:00+00:00"}, after=after)
    assert nxt is None


def test_compute_next_run_weekday_cron():
    # Lundi à 9h — cron weekday 1 = lundi. Depuis un mardi, prochaine
    # échéance : lundi suivant.
    after = datetime(2026, 1, 6, 10, 0, tzinfo=timezone.utc)  # mardi
    nxt = wt.compute_next_run({"cron": "0 9 * * 1", "timezone": "UTC"}, after=after)
    assert nxt.weekday() == 0  # lundi (datetime.weekday(): 0=lundi)
    assert nxt.hour == 9
    assert nxt > after


# --- tick : lance un run échu -----------------------------------------------


class _Recorder:
    def __init__(self, run_id="run-tick-1", final_status="success"):
        self.calls = []
        self.tasks = []
        self.run_id = run_id
        self.final_status = final_status

    async def __call__(self, *, workflow_id, owner_id, kind, detail, payload):
        self.calls.append(
            dict(workflow_id=workflow_id, kind=kind, detail=detail, payload=payload)
        )

        async def _noop():
            return None

        task = asyncio.create_task(_noop())
        self.tasks.append(task)
        return self.run_id, task


async def _settle():
    # Laisse les callbacks planifiés par add_done_callback (call_soon)
    # s'exécuter après que la tâche factice s'est terminée.
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def _publish_schedule(cron="*/5 * * * *"):
    wf = wm.create_workflow(owner_id=ALICE, name="W", graph=_schedule_graph(cron=cron))
    wm.update_workflow(
        wf["workflow_id"],
        owner_id=ALICE,
        expected_version=wf["version"],
        status="published",
    )
    return wf["workflow_id"]


async def test_tick_fires_a_due_schedule_trigger(monkeypatch):
    wid = await _publish_schedule()
    past = datetime(2000, 1, 1, tzinfo=timezone.utc)
    _set_row(wid, next_run_at=past.isoformat())

    rec = _Recorder()
    monkeypatch.setattr(wt, "launch_triggered_run", rec)
    # ``fire_schedule_trigger._on_done`` relit l'issue via ``run_main.get_run``
    # (import différé) : sans ce double, il chercherait le run "run-tick-1"
    # dans le vrai Postgres des réglages par défaut, absent du bac à sable.
    monkeypatch.setattr(
        "apowerb.core.run_main.get_run",
        lambda run_id, owner_id: {"status": "success"},
    )

    now = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    fired = await flow_scheduler.tick_once(now=now)
    await _settle()

    assert fired == 1
    assert rec.calls[0]["workflow_id"] == wid
    assert rec.calls[0]["kind"] == "schedule"
    assert rec.calls[0]["payload"] == {"scheduled_at": now.isoformat()}

    row = _row(wid)
    assert row["last_run_id"] == "run-tick-1"
    assert row["last_fired_at"] is not None
    # next_run_at a avancé (n'est plus l'échéance passée qu'on a forcée).
    assert row["next_run_at"] != past.isoformat()


async def test_tick_is_skipped_when_the_previous_run_is_still_running(monkeypatch):
    wid = await _publish_schedule()
    past = datetime(2000, 1, 1, tzinfo=timezone.utc)
    _set_row(wid, next_run_at=past.isoformat(), last_status="running")

    rec = _Recorder()
    monkeypatch.setattr(wt, "launch_triggered_run", rec)

    now = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    fired = await flow_scheduler.tick_once(now=now)

    assert fired == 0
    assert rec.calls == []  # aucun run lancé : chevauchement évité
    row = _row(wid)
    assert (
        row["last_status"] == "running"
    )  # inchangé : le run précédent tourne toujours
    # L'échéance a quand même avancé, pour ne pas retenter indéfiniment le
    # même créneau manqué à chaque passage de la boucle.
    assert row["next_run_at"] != past.isoformat()


async def test_fire_schedule_trigger_reserves_the_tick_atomically(monkeypatch):
    # Deux "réplicas" qui ont lu la MÊME ligne échue (même next_run_at) avant
    # que l'un des deux ait eu le temps d'avancer l'échéance en base : seule
    # la réservation atomique (UPDATE conditionné sur le next_run_at lu) doit
    # empêcher un double lancement.
    wid = await _publish_schedule()
    past = datetime(2000, 1, 1, tzinfo=timezone.utc)
    _set_row(wid, next_run_at=past.isoformat())

    rec = _Recorder()
    monkeypatch.setattr(wt, "launch_triggered_run", rec)
    monkeypatch.setattr(
        "apowerb.core.run_main.get_run",
        lambda run_id, owner_id: {"status": "success"},
    )

    now = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    same_row_read_by_both_replicas = _row(wid)

    result_a = await wt.fire_schedule_trigger(same_row_read_by_both_replicas, now=now)
    result_b = await wt.fire_schedule_trigger(same_row_read_by_both_replicas, now=now)
    await _settle()

    assert (result_a, result_b) == (True, False)
    # un seul lancement malgré 2 tentatives sur la même lecture
    assert len(rec.calls) == 1

    row_after = _row(wid)
    # next_run_at avancé UNE seule fois (pas re-avancé deux fois depuis
    # ``past``) ; la seconde tentative n'a rien écrit.
    assert row_after["next_run_at"] != past.isoformat()


async def test_tick_ignores_a_trigger_that_is_not_yet_due():
    wid = await _publish_schedule()
    far_future = datetime(2099, 1, 1, tzinfo=timezone.utc)
    _set_row(wid, next_run_at=far_future.isoformat())

    due = wt.due_schedule_triggers(datetime(2026, 3, 1, tzinfo=timezone.utc))
    assert wid not in {r["workflow_id"] for r in due}

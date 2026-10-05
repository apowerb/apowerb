"""A schedule stuck at ``last_status="running"`` after a crash must recover.

``fire_schedule_trigger`` sets ``last_status="running"`` before launching and
only the in-memory ``_on_done`` callback clears it. A process kill/restart
loses that callback, so the row stays ``running`` forever and the overlap
guard skips every later slot — the trigger is frozen for life (#255 covered
the launch-raises case only).

The reconciliation lifts the guard when the previous run is orphaned (its run
row is terminal/absent, or the ``running`` is older than the stale window),
while still skipping a run that is genuinely in flight.
"""

from datetime import datetime, timedelta, timezone

from apowerb.core import flow_scheduler
from apowerb.core import workflow_triggers as wt
from tests.test_workflow_triggers_schedule import (  # noqa: F401
    _Recorder, _publish_schedule, _row, _set_row, _settle, store,
)

PAST = datetime(2000, 1, 1, tzinfo=timezone.utc).isoformat()
NOW = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


async def test_running_with_a_terminal_run_recovers(monkeypatch):
    """The previous run finished but the done-callback was lost (restart):
    the trigger must fire again."""
    wid = await _publish_schedule()
    _set_row(wid, last_status="running", last_run_id="dead", next_run_at=PAST)

    def fake_get_run(run_id, owner_id):
        return {"status": "error"} if run_id == "dead" else {"status": "success"}

    monkeypatch.setattr("apowerb.core.run_main.get_run", fake_get_run)
    rec = _Recorder()
    monkeypatch.setattr(wt, "launch_triggered_run", rec)

    fired = await flow_scheduler.tick_once(now=NOW)
    await _settle()

    assert fired == 1, "a schedule stuck on a finished run stayed frozen"
    assert _row(wid)["last_status"] != "running"


async def test_running_in_flight_is_still_skipped(monkeypatch):
    """Discriminant: a run genuinely in flight within the stale window must
    still be skipped — the overlap guard must survive the reconciliation."""
    wid = await _publish_schedule()
    _set_row(
        wid,
        last_status="running",
        last_run_id="live",
        last_fired_at=NOW.isoformat(),  # just started
        next_run_at=PAST,
    )
    monkeypatch.setattr(
        "apowerb.core.run_main.get_run", lambda run_id, owner_id: {"status": "running"}
    )
    rec = _Recorder()
    monkeypatch.setattr(wt, "launch_triggered_run", rec)

    fired = await flow_scheduler.tick_once(now=NOW)
    await _settle()

    assert fired == 0, "a genuinely running schedule was double-fired"
    assert rec.calls == []
    assert _row(wid)["last_status"] == "running"


async def test_running_stuck_past_ttl_recovers(monkeypatch):
    """A run killed mid-flight leaves its own row at ``running`` too, so the
    run-status check can't help; the age backstop must unfreeze it."""
    wid = await _publish_schedule()
    stale_since = (NOW - wt._RUNNING_STALE_AFTER - timedelta(hours=1)).isoformat()
    _set_row(
        wid,
        last_status="running",
        last_run_id="stuck",
        last_fired_at=stale_since,
        next_run_at=PAST,
    )

    def fake_get_run(run_id, owner_id):
        # The old run is itself stuck "running"; the new one finishes.
        return {"status": "running"} if run_id == "stuck" else {"status": "success"}

    monkeypatch.setattr("apowerb.core.run_main.get_run", fake_get_run)
    rec = _Recorder()
    monkeypatch.setattr(wt, "launch_triggered_run", rec)

    fired = await flow_scheduler.tick_once(now=NOW)
    await _settle()

    assert fired == 1, "a schedule stuck past the stale window stayed frozen"
    assert _row(wid)["last_status"] != "running"

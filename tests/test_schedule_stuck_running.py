"""A schedule trigger whose launch raises must not freeze: later slots fire.

``fire_schedule_trigger`` writes ``last_status="running"`` before launching;
only the in-memory ``_on_done`` callback (or ``TriggerNotActive``) clears it.
"""

from datetime import datetime, timedelta, timezone

from apowerb.core import flow_scheduler
from apowerb.core import workflow_triggers as wt
from tests.test_workflow_triggers_schedule import (  # noqa: F401
    _Recorder, _publish_schedule, _row, _set_row, _settle, store,
)


async def test_a_failed_launch_does_not_freeze_the_schedule(monkeypatch):
    wid = await _publish_schedule()
    _set_row(wid, next_run_at=datetime(2000, 1, 1, tzinfo=timezone.utc).isoformat())

    async def launch_fails(**_):
        raise RuntimeError("database blip while starting the run")

    monkeypatch.setattr(wt, "launch_triggered_run", launch_fails)
    now = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    try:
        await flow_scheduler.tick_once(now=now)
    except Exception:
        pass
    await _settle()
    print(f"\n[after failed launch] last_status={_row(wid)['last_status']!r}")

    rec = _Recorder()
    monkeypatch.setattr(wt, "launch_triggered_run", rec)
    monkeypatch.setattr("apowerb.core.run_main.get_run", lambda run_id, owner_id: {"status": "success"})
    fired = []
    for day in range(1, 4):  # three later due slots, database back to normal
        _set_row(wid, next_run_at=datetime(2000, 1, 1, tzinfo=timezone.utc).isoformat())
        fired.append(await flow_scheduler.tick_once(now=now + timedelta(days=day)))
        await _settle()
    print(f"[next 3 slots] fired={fired} last_status={_row(wid)['last_status']!r}")
    assert sum(fired) == 3

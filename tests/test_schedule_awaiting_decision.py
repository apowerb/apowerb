"""Un planificateur n'empile pas les runs en attente d'une décision humaine.

Tant que le dernier run lancé par le trigger attend sa décision, les créneaux
suivants sont sautés (l'échéance avance) ; dès qu'il est repris ou abandonné,
le planificateur repart.
"""

from datetime import datetime, timezone

import pytest

from apowerb.core import flow_scheduler
from apowerb.core import workflow_triggers as wt
from tests.test_workflow_triggers_schedule import (  # noqa: F401
    _publish_schedule,
    _Recorder,
    _row,
    _set_row,
    _settle,
    store,
)

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
DUE = datetime(2000, 1, 1, tzinfo=timezone.utc).isoformat()


@pytest.mark.parametrize(
    "status, fires", [("awaiting", 0), ("running", 0), ("success", 1), ("cancelled", 1), ("error", 1)]
)
async def test_a_schedule_waits_while_its_last_run_waits_for_a_decision(
    monkeypatch, status, fires
):
    wid = await _publish_schedule()
    _set_row(wid, next_run_at=DUE, last_status="awaiting", last_run_id="r-paused")
    monkeypatch.setattr(
        "apowerb.core.run_main.get_run", lambda run_id, owner_id: {"status": status}
    )
    rec = _Recorder()
    monkeypatch.setattr(wt, "launch_triggered_run", rec)
    fired = await flow_scheduler.tick_once(now=NOW)
    await _settle()
    assert fired == fires
    assert _row(wid)["next_run_at"] != DUE  # le créneau est consommé dans tous les cas

"""A run killed mid-flight never runs settle_run's ``finally``, so it stays
``running`` with no ``finished_at`` forever. That froze two things: its replay
returned 409 "still in flight" for good, and its listed status lied. Reads now
settle such a run to ``error`` once it is older than ``RUN_STALE_AFTER``.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from fastapi import HTTPException

from apowerb.core import run_main

OWNER = "u@example.com"
MESSAGE = {"role": "user", "parts": [{"text": "Hello"}]}


@pytest.fixture
def store(monkeypatch, tmp_path):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def _attach_public_schema(dbapi_connection, _record):  # pragma: no cover
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS public")

    monkeypatch.setattr(run_main.run_store, "engine", engine)
    run_main.run_store.metadata.create_all(engine)
    monkeypatch.setattr(run_main, "_run_input_dir", lambda run_id: tmp_path / run_id)
    return run_main.run_store


def _running(trigger="workflow", *, agent_ids=None, config=None, age_hours=0.0) -> str:
    """A live ``running`` run, back-dated ``age_hours`` into the past."""
    run_id = run_main.start_run(
        owner_id=OWNER,
        trigger=trigger,
        agent_ids=agent_ids if agent_ids is not None else ["3"],
        config=config or {},
    )
    if age_hours:
        backdated = (datetime.now() - timedelta(hours=age_hours)).strftime(
            "%Y-%m-%d %H:%M:%S.%f"
        )
        with run_main.run_store.engine.begin() as conn:
            conn.execute(
                run_main.run_store.run_table.update()
                .where(run_main.run_store.run_table.c.run_id == run_id)
                .values(created_at=backdated)
            )
    return run_id


def test_an_orphaned_running_run_is_settled_to_error(store):
    run_id = _running(age_hours=7)

    run = run_main.get_run(run_id, owner_id=OWNER)

    assert run["status"] == "error"
    assert run["finished_at"]
    assert "reconciliation" in (run["error_message"] or "")


def test_a_running_run_without_a_readable_date_is_left_untouched(store):
    # No age signal -> fail safe towards inaction: never settle it.
    run_id = _running(age_hours=0)
    with run_main.run_store.engine.begin() as conn:
        conn.execute(
            run_main.run_store.run_table.update()
            .where(run_main.run_store.run_table.c.run_id == run_id)
            .values(created_at=None)
        )

    run = run_main.get_run(run_id, owner_id=OWNER)
    assert run["status"] == "running"
    assert not run["finished_at"]


def test_a_recent_running_run_is_left_in_flight(store):
    run_id = _running(age_hours=0)  # just started

    run = run_main.get_run(run_id, owner_id=OWNER)
    assert run["status"] == "running"

    with pytest.raises(HTTPException) as exc:
        run_main.prepare_replay(run_id, owner_id=OWNER)
    assert exc.value.status_code == 409
    assert "still in flight" in exc.value.detail


def test_list_runs_reconciles_an_orphan(store):
    run_id = _running(age_hours=7)

    [listed] = run_main.list_runs(owner_id=OWNER)
    assert listed["run_id"] == run_id
    assert listed["status"] == "error"


def test_an_orphaned_non_agent_run_becomes_replayable(store):
    run_id = _running(
        trigger="workflow",
        agent_ids=[],
        config={"workflow_id": "wf1", "version": "1"},
        age_hours=7,
    )

    replay = run_main.prepare_replay(run_id, owner_id=OWNER)
    assert replay["run_id"] != run_id  # a fresh run was opened


def test_an_orphaned_agent_run_still_guards_side_effects(store):
    # A chat/schedule run acts through tools; an orphan never recorded which,
    # so a bare replay is refused for side effects -- NOT "still in flight".
    run_id = _running(
        trigger="chat",
        agent_ids=["support-agent"],
        config={"agent_name": "support-agent", "new_message": MESSAGE},
        age_hours=7,
    )

    with pytest.raises(HTTPException) as exc:
        run_main.prepare_replay(run_id, owner_id=OWNER)
    assert exc.value.status_code == 409
    assert "still in flight" not in exc.value.detail
    assert "force" in exc.value.detail

    # force overrides the side-effect guard on a now-replayable run
    replay = run_main.prepare_replay(run_id, owner_id=OWNER, force=True)
    assert replay["run_id"] != run_id

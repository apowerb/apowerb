"""A run killed mid-flight must not stay ``running`` for life.

``finish_run`` settles a run from the process that runs it; a kill/restart loses
that write, so the row stays ``running`` with ``finished_at`` empty forever.
``get_run``/``list_runs`` then show it in flight indefinitely and
``prepare_replay`` answers 409 "still in flight" — the user can never replay it.

The readers reconcile such a row to ``error`` once it is older than the stale
window. These pin that: an old ``running`` row flips (RED without the fix), a
recent one is left alone (discriminant), and a settled row is never touched.
``created_at`` is written by ``_now`` in local naive time, so the tests build
timestamps the same way — a UTC cutoff would be off by the server's offset.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from apowerb.core import run_main

OWNER = "u@example.com"


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


# The literal format run_main writes created_at/finished_at in (local naive).
# Hard-coded here on purpose: the test must build timestamps without importing a
# symbol the fix introduces, so its RED is behavioural, not a missing name.
_TS_FORMAT = "%Y-%m-%d %H:%M:%S.%f"


def _ts(age: timedelta) -> str:
    """A ``created_at`` string ``age`` ago, in run_main's own local format."""
    return (datetime.now() - age).strftime(_TS_FORMAT)


def _insert(
    store,
    *,
    status: str,
    created_age: timedelta,
    finished: bool = False,
    trigger: str = "workflow",
    run_id: str | None = None,
) -> str:
    run_id = run_id or run_main.nanoid_generate(size=21)
    with store.engine.begin() as conn:
        conn.execute(
            store.run_table.insert().values(
                run_id=run_id,
                trigger=trigger,
                owner_id=OWNER,
                agent_ids="[]",
                config="{}",
                status=status,
                attempts=1,
                created_at=_ts(created_age),
                finished_at=_ts(created_age) if finished else None,
            )
        )
    return run_id


def test_get_run_flips_a_stale_running_row_to_error(store):
    run_id = _insert(store, status="running", created_age=timedelta(hours=7))

    run = run_main.get_run(run_id, owner_id=OWNER)

    assert run["status"] == "error"
    assert "orphaned" in (run["error_message"] or "").lower()
    assert run["finished_at"] is not None


def test_get_run_leaves_a_recent_running_row_in_flight(store):
    run_id = _insert(store, status="running", created_age=timedelta(hours=1))

    run = run_main.get_run(run_id, owner_id=OWNER)

    assert run["status"] == "running"
    assert run["finished_at"] is None


def test_a_settled_row_older_than_the_window_is_untouched(store):
    run_id = _insert(
        store, status="success", created_age=timedelta(hours=9), finished=True
    )

    run = run_main.get_run(run_id, owner_id=OWNER)

    assert run["status"] == "success"


def test_list_runs_flips_only_the_stale_running_rows(store):
    stale = _insert(store, status="running", created_age=timedelta(hours=8))
    fresh = _insert(store, status="running", created_age=timedelta(minutes=30))
    done = _insert(
        store, status="success", created_age=timedelta(hours=8), finished=True
    )

    by_id = {r["run_id"]: r for r in run_main.list_runs(owner_id=OWNER)}

    assert by_id[stale]["status"] == "error"
    assert by_id[fresh]["status"] == "running"
    assert by_id[done]["status"] == "success"


def test_prepare_replay_no_longer_blocks_an_orphaned_run(store):
    # A non-agent trigger keeps input on disk; an orphaned run becomes 'error',
    # which is replayable, so prepare_replay must not answer 409 "in flight".
    run_id = run_main.start_run(
        trigger="workflow",
        owner_id=OWNER,
        file_bytes=b"payload",
        file_name="in.txt",
    )
    # Backdate it past the stale window, still 'running'.
    with store.engine.begin() as conn:
        conn.execute(
            store.run_table.update()
            .where(store.run_table.c.run_id == run_id)
            .values(created_at=_ts(timedelta(hours=7)))
        )

    replay = run_main.prepare_replay(run_id, owner_id=OWNER)

    assert replay["file_bytes"] == b"payload"

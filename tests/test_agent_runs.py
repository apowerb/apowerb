"""Agent runs: an execution that fails must be findable, and replayable.

Reprise after failure existed only on the webhook path, and only for Outlook:
``webhook_logs`` carries the payload, the attempt count and the stored
attachments, so a message can be re-processed. A run started anywhere else —
the workflow canvas, a schedule, an API call — left nothing behind. The
workflow router kept its state in a process-local dict and dropped it when the
stream ended, so nothing survived a restart, let alone a crash.

These tests use a real SQLite engine rather than fakes: what is asserted is
what a later process could read back, which is the whole point of the feature.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.pool import StaticPool

from apowerb.core import run_main

OWNER = "u@example.com"
OTHER = "someone-else@example.com"


def _sqlite_engine():
    """In-memory engine that answers to whichever schema the store uses.

    ``DB_SCHEMA`` decides it: "public" by default (Postgres in production),
    empty on the CI bench. SQLite reaches a named schema through an ATTACHed
    database, so ``public`` is attached unconditionally — harmless when
    nothing is qualified with it. StaticPool keeps the single connection
    alive, so what is written survives between ``engine.begin()`` blocks.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _attach_public_schema(dbapi_connection, _record):  # pragma: no cover
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS public")

    return engine


@pytest.fixture
def store(monkeypatch, tmp_path):
    """The real RunStore, pointed at SQLite, with its tables created."""
    engine = _sqlite_engine()
    run_store = run_main.run_store
    monkeypatch.setattr(run_store, "engine", engine)
    run_store.metadata.create_all(engine)
    # Keep the preserved input out of the repository's uploads directory.
    monkeypatch.setattr(run_main, "_run_input_dir", lambda run_id: tmp_path / run_id)
    return run_store


def _rows(store):
    with store.engine.begin() as conn:
        rows = conn.execute(
            select(store.run_table).order_by(store.run_table.c.created_at)
        ).fetchall()
    return [r._asdict() for r in rows]


def _start(**overrides):
    kwargs = {
        "trigger": "workflow",
        "owner_id": OWNER,
        "agent_ids": ["3", "7"],
        "config": {"rows": 2},
    }
    kwargs.update(overrides)
    return run_main.start_run(**kwargs)


def test_a_run_is_recorded_the_moment_it_starts(store):
    run_id = _start()

    rows = _rows(store)
    assert len(rows) == 1
    assert rows[0]["run_id"] == run_id
    assert rows[0]["status"] == "running"
    assert rows[0]["trigger"] == "workflow"
    assert json.loads(rows[0]["agent_ids"]) == ["3", "7"]


def test_a_failed_run_keeps_the_cause_readable_afterwards(store):
    run_id = _start()

    run_main.finish_run(run_id, status="error", error_message="LLM provider 429")

    run = run_main.get_run(run_id, owner_id=OWNER)
    assert run["status"] == "error"
    assert run["error_message"] == "LLM provider 429"
    assert run["finished_at"]


def test_replaying_a_failed_run_reuses_the_input_it_kept(store):
    run_id = _start(file_bytes=b"colonne;valeur\na;1\n", file_name="entree.csv")
    run_main.finish_run(run_id, status="error", error_message="boom")

    replay = run_main.prepare_replay(run_id, owner_id=OWNER)

    assert replay["agent_ids"] == ["3", "7"]
    assert replay["config"] == {"rows": 2}
    assert replay["file_bytes"] == b"colonne;valeur\na;1\n"
    assert replay["file_name"] == "entree.csv"


def test_a_replay_is_a_new_run_that_points_back_to_the_original(store):
    run_id = _start()
    run_main.finish_run(run_id, status="error", error_message="boom")

    replay = run_main.prepare_replay(run_id, owner_id=OWNER)

    assert replay["run_id"] != run_id
    fresh = run_main.get_run(replay["run_id"], owner_id=OWNER)
    assert fresh["replay_of"] == run_id
    assert fresh["status"] == "running"
    # The original is left exactly as it was: a replay does not rewrite history.
    assert run_main.get_run(run_id, owner_id=OWNER)["status"] == "error"


def test_a_successful_run_is_not_replayable_by_accident(store):
    """Its side effects already happened — re-running them must be deliberate."""
    run_id = _start()
    run_main.finish_run(run_id, status="success")

    with pytest.raises(Exception) as excinfo:
        run_main.prepare_replay(run_id, owner_id=OWNER)
    assert getattr(excinfo.value, "status_code", None) == 409

    forced = run_main.prepare_replay(run_id, owner_id=OWNER, force=True)
    assert forced["run_id"] != run_id


def test_a_run_still_in_flight_is_not_replayable(store):
    run_id = _start()

    with pytest.raises(Exception) as excinfo:
        run_main.prepare_replay(run_id, owner_id=OWNER)
    assert getattr(excinfo.value, "status_code", None) == 409


def test_a_foreign_owner_can_neither_read_nor_replay(store):
    run_id = _start()
    run_main.finish_run(run_id, status="error", error_message="boom")

    assert run_main.get_run(run_id, owner_id=OTHER) is None
    assert run_main.list_runs(owner_id=OTHER) == []
    with pytest.raises(Exception) as excinfo:
        run_main.prepare_replay(run_id, owner_id=OTHER)
    assert getattr(excinfo.value, "status_code", None) == 404


def test_runs_are_listed_newest_first_for_their_owner(store):
    first = _start()
    run_main.finish_run(first, status="success")
    second = _start()

    listed = run_main.list_runs(owner_id=OWNER)
    assert [r["run_id"] for r in listed] == [second, first]


# ---------------------------------------------------------------------------
# The workflow endpoint itself — the run must be recorded, and its outcome too
# ---------------------------------------------------------------------------


async def _drain(response):
    """Consume a StreamingResponse body the way the ASGI server would."""
    return b"".join([chunk async for chunk in response.body_iterator])


def _user(email=OWNER):
    user = SimpleNamespace(email=email, user_id=1, role="USER")
    return user


@pytest.fixture
def workflows(monkeypatch, store):
    from apowerb.routers import workflows as module

    module._runs.clear()

    async def _runner(wid, cancel_event, canvas_agent_ids, file_bytes):
        yield f"data: {json.dumps({'event': 'started', 'wid': wid})}\n\n"
        yield f"data: {json.dumps({'event': 'done'})}\n\n"

    monkeypatch.setattr(module, "_workflow_runner", _runner)
    return module


@pytest.mark.asyncio
async def test_the_endpoint_records_the_run_and_how_it_ended(workflows):
    response = await workflows.run_workflow_sse(
        canvas_agent_ids=json.dumps(["3"]),
        workflow_id="wid-abc",
        config_json=json.dumps({"rows": 1}),
        file=None,
        current_user=_user(),
    )
    await _drain(response)

    run = run_main.get_run("wid-abc", owner_id=OWNER)
    assert run is not None, "the run was never recorded"
    assert run["status"] == "success"
    assert run["trigger"] == "workflow"
    assert run["agent_ids"] == ["3"]


@pytest.mark.asyncio
async def test_a_runner_that_raises_leaves_the_cause_in_the_record(workflows, monkeypatch):
    """The whole point: a failed run must still be findable afterwards."""

    async def _exploding_runner(wid, cancel_event, canvas_agent_ids, file_bytes):
        yield f"data: {json.dumps({'event': 'started', 'wid': wid})}\n\n"
        raise RuntimeError("provider refused the call")

    monkeypatch.setattr(workflows, "_workflow_runner", _exploding_runner)

    response = await workflows.run_workflow_sse(
        canvas_agent_ids=json.dumps(["3"]),
        workflow_id="wid-boom",
        config_json=None,
        file=None,
        current_user=_user(),
    )
    body = await _drain(response)

    assert b"provider refused the call" in body
    run = run_main.get_run("wid-boom", owner_id=OWNER)
    assert run["status"] == "error"
    assert "provider refused the call" in run["error_message"]


@pytest.mark.asyncio
async def test_the_endpoint_replays_a_failed_run_as_a_new_one(workflows):
    run_id = _start()
    run_main.finish_run(run_id, status="error", error_message="boom")

    response = await workflows.replay_run(run_id, force=False, current_user=_user())
    await _drain(response)

    runs = run_main.list_runs(owner_id=OWNER)
    assert len(runs) == 2
    replay = [r for r in runs if r["replay_of"] == run_id]
    assert len(replay) == 1
    assert replay[0]["status"] == "success"
    # The original keeps its own verdict.
    assert run_main.get_run(run_id, owner_id=OWNER)["status"] == "error"


@pytest.mark.asyncio
async def test_a_storage_failure_does_not_stop_the_workflow(workflows, monkeypatch):
    """Losing the trace degrades the feature; it must not take the product down."""

    def _broken_start_run(**kwargs):
        raise RuntimeError("database is down")

    monkeypatch.setattr(run_main, "start_run", _broken_start_run)

    response = await workflows.run_workflow_sse(
        canvas_agent_ids=json.dumps(["3"]),
        workflow_id="wid-nodb",
        config_json=None,
        file=None,
        current_user=_user(),
    )
    body = await _drain(response)

    assert b"run_started" in body
    assert b"done" in body


def test_an_existing_database_still_gets_the_runs_table(monkeypatch):
    """The DDL trap: create_all must not be skipped because a table exists."""
    engine = _sqlite_engine()
    run_store = run_main.run_store
    monkeypatch.setattr(run_store, "engine", engine)

    run_store.create_table()

    from sqlalchemy import inspect as sa_inspect

    schema = run_store.run_table.schema
    assert "agent_runs" in sa_inspect(engine).get_table_names(schema=schema)

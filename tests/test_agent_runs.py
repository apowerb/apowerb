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


# ---------------------------------------------------------------------------
# Runs started by the scheduler (Mage -> run_agent_from_refresh_token)
# ---------------------------------------------------------------------------
#
# The scheduler reaches the ADK server directly, outside /workflows/run-sse:
# before this, a scheduled run that failed left a log line and nothing else.


SCHEDULED_TOKEN = {
    "agent_name": "reporting-agent",
    "user_id": OWNER,
    "session_id": "sched-base",
    "new_message": {"role": "user", "content": "Send the weekly report"},
    "run_mode": "single",
    "streaming": False,
}


def _call(name):
    return {"content": {"role": "model", "parts": [{"functionCall": {"name": name, "args": {}}}]}}


@pytest.fixture
def scheduler(monkeypatch, store):
    """The real scheduler entry point, with the ADK server faked out.

    ``run_adk`` and ``session_events`` are what the fake server answers; tests
    set them before calling ``run``.
    """
    from unittest.mock import AsyncMock

    from apowerb.core import adk_runner as core_adk
    from apowerb.core import run_gate
    from apowerb.helpers import security
    from apowerb.scheduler import run_agent_background as rab

    state = SimpleNamespace(run_adk=AsyncMock(return_value=[]), session_events=[])

    async def _get_session(**kwargs):
        return {"id": kwargs.get("session_id"), "events": state.session_events}

    async def _no_guard(**kwargs):
        return None

    async def _plan(owner):
        return None

    monkeypatch.setattr(rab, "decode_agent_refresh_token", lambda token: dict(SCHEDULED_TOKEN))
    monkeypatch.setattr(rab, "get_agent_folder_name", lambda name: "reporting_agent")
    monkeypatch.setattr(security, "refresh_access_token_from_agent_refresh", lambda token: "access")
    monkeypatch.setattr(core_adk, "get_adk_session", _get_session)
    monkeypatch.setattr(core_adk, "create_adk_agent_session", AsyncMock(return_value={}))
    monkeypatch.setattr(run_gate, "apply_run_guards", _no_guard)
    monkeypatch.setattr(run_gate, "resolve_owner_plan", _plan)
    monkeypatch.setattr(rab, "run_adk_agent", lambda **kwargs: state.run_adk(**kwargs))
    # The replay goes through the core runner, not the scheduler package.
    monkeypatch.setattr(core_adk, "run_adk_agent", lambda **kwargs: state.run_adk(**kwargs))

    async def _run():
        return await rab.run_agent_from_refresh_token("refresh", agent_id="agent-42")

    state.run = _run
    return state


@pytest.mark.asyncio
async def test_a_failed_scheduled_run_is_listed_with_its_cause(scheduler):
    scheduler.run_adk.side_effect = RuntimeError("provider answered 503")

    with pytest.raises(RuntimeError):
        await scheduler.run()

    [run] = run_main.list_runs(owner_id=OWNER)
    assert run["trigger"] == "schedule"
    assert run["status"] == "error"
    assert "provider answered 503" in run["error_message"]
    assert run["agent_ids"] == ["agent-42"]
    # The input the replay will need: which agent, and the exact message.
    assert run["config"]["agent_name"] == "reporting-agent"
    assert run["config"]["new_message"]["parts"][0]["text"] == "Send the weekly report"
    # No tool ran before the failure: nothing stands in the way of a replay.
    assert run["tools_executed"] == []


@pytest.mark.asyncio
async def test_a_successful_scheduled_run_is_not_replayable_as_a_failure(scheduler):
    """Counter-example: success and failure must not end up in the same state."""
    scheduler.run_adk.return_value = [_call("send_email")]

    await scheduler.run()

    [run] = run_main.list_runs(owner_id=OWNER)
    assert run["status"] == "success"
    assert run["error_message"] is None
    assert run["tools_executed"] == ["send_email"]
    with pytest.raises(Exception) as excinfo:
        run_main.prepare_replay(run["run_id"], owner_id=OWNER)
    assert getattr(excinfo.value, "status_code", None) == 409


@pytest.mark.asyncio
async def test_a_failed_run_whose_tools_already_ran_is_not_replayed_silently(scheduler):
    """The e-mail went out before the crash: replaying would send it twice."""
    scheduler.run_adk.side_effect = RuntimeError("model crashed after the tool")
    scheduler.session_events = [_call("send_email")]

    with pytest.raises(RuntimeError):
        await scheduler.run()

    [run] = run_main.list_runs(owner_id=OWNER)
    assert run["status"] == "error"
    assert run["tools_executed"] == ["send_email"]
    with pytest.raises(Exception) as excinfo:
        run_main.prepare_replay(run["run_id"], owner_id=OWNER)
    assert excinfo.value.status_code == 409
    assert "send_email" in str(excinfo.value.detail)
    # A deliberate gesture still goes through.
    assert run_main.prepare_replay(run["run_id"], owner_id=OWNER, force=True)["run_id"]


def test_an_agent_run_whose_side_effects_are_unknown_is_not_replayed(store):
    """Unknown is not "none": the trace of the tools could not be read."""
    run_id = _start(trigger="schedule", config={"agent_name": "a", "new_message": {}})
    run_main.finish_run(run_id, status="error", error_message="boom")

    with pytest.raises(Exception) as excinfo:
        run_main.prepare_replay(run_id, owner_id=OWNER)
    assert excinfo.value.status_code == 409


@pytest.mark.asyncio
async def test_a_scheduled_run_is_invisible_to_another_user(scheduler):
    scheduler.run_adk.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        await scheduler.run()
    [run] = run_main.list_runs(owner_id=OWNER)

    assert run_main.list_runs(owner_id=OTHER) == []
    assert run_main.get_run(run["run_id"], owner_id=OTHER) is None
    with pytest.raises(Exception) as excinfo:
        run_main.prepare_replay(run["run_id"], owner_id=OTHER)
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_a_storage_failure_does_not_stop_a_scheduled_run(scheduler, monkeypatch):
    def _broken(**kwargs):
        raise RuntimeError("database is down")

    monkeypatch.setattr(run_main, "start_run", _broken)
    scheduler.run_adk.return_value = []

    result = await scheduler.run()

    assert result["success"] is True


@pytest.mark.asyncio
async def test_replaying_a_failed_scheduled_run_sends_its_message_again(scheduler, workflows, monkeypatch):
    """The endpoint must replay an agent run as an agent run, not as a canvas."""
    scheduler.run_adk.side_effect = RuntimeError("provider answered 503")
    with pytest.raises(RuntimeError):
        await scheduler.run()
    [original] = run_main.list_runs(owner_id=OWNER)
    original_session = scheduler.run_adk.await_args.kwargs["session_id"]

    async def _canvas_must_not_run(*args, **kwargs):
        raise AssertionError("an agent run was replayed as a workflow canvas")
        yield  # pragma: no cover

    monkeypatch.setattr(workflows, "_workflow_runner", _canvas_must_not_run)
    from apowerb.core import agent_main

    monkeypatch.setattr(agent_main, "get_agent_folder_name", lambda name: "reporting_agent")
    scheduler.run_adk.side_effect = None
    scheduler.run_adk.return_value = [
        {"content": {"role": "model", "parts": [{"text": "Report sent."}]}}
    ]

    response = await workflows.replay_run(original["run_id"], force=False, current_user=_user())
    body = await _drain(response)

    assert b"Report sent." in body
    kwargs = scheduler.run_adk.await_args.kwargs
    assert kwargs["agent_name"] == "reporting_agent"
    assert kwargs["user_id"] == OWNER
    assert kwargs["new_message"]["parts"][0]["text"] == "Send the weekly report"
    # A fresh session: the failed one may hold a half-done conversation.
    assert kwargs["session_id"] != original_session
    [replay] = [r for r in run_main.list_runs(owner_id=OWNER) if r["replay_of"]]
    assert replay["replay_of"] == original["run_id"]
    assert replay["trigger"] == "schedule"
    assert replay["status"] == "success"
    assert replay["tools_executed"] == []
    assert run_main.get_run(original["run_id"], owner_id=OWNER)["status"] == "error"


@pytest.mark.asyncio
async def test_a_replay_that_fails_again_is_recorded_as_a_failure(scheduler, workflows, monkeypatch):
    scheduler.run_adk.side_effect = RuntimeError("first failure")
    with pytest.raises(RuntimeError):
        await scheduler.run()
    [original] = run_main.list_runs(owner_id=OWNER)
    from apowerb.core import agent_main

    monkeypatch.setattr(agent_main, "get_agent_folder_name", lambda name: "reporting_agent")
    scheduler.run_adk.side_effect = RuntimeError("second failure")

    response = await workflows.replay_run(original["run_id"], force=False, current_user=_user())
    await _drain(response)

    [replay] = [r for r in run_main.list_runs(owner_id=OWNER) if r["replay_of"]]
    assert replay["status"] == "error"
    assert "second failure" in replay["error_message"]
    assert replay["tools_executed"] == []


def test_an_existing_runs_table_gets_the_tools_column(monkeypatch):
    """The DDL trap again: the column is new, the table is not."""
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text

    engine = _sqlite_engine()
    run_store = run_main.run_store
    monkeypatch.setattr(run_store, "engine", engine)
    schema = run_store.run_table.schema
    qualified = f"{schema}.agent_runs" if schema else "agent_runs"
    with engine.begin() as conn:
        conn.execute(text(f"CREATE TABLE {qualified} (run_id VARCHAR PRIMARY KEY, status VARCHAR)"))

    run_store.create_table()

    columns = {c["name"] for c in sa_inspect(engine).get_columns("agent_runs", schema=schema)}
    assert "tools_executed" in columns

"""``input_available`` says whether a run still has what a replay needs.

A chat or scheduled run keeps its input in ``config`` (the agent and the
message), never on disk: its ``input_file_path`` is always empty. Reading only
that column reported every agent run as not replayable, while the replay itself
worked.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

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


def _failed(**kwargs) -> str:
    run_id = run_main.start_run(owner_id=OWNER, **kwargs)
    run_main.finish_run(run_id, status="error", error_message="boom")
    return run_id


@pytest.mark.parametrize("trigger", ["chat", "schedule"])
def test_an_agent_run_that_kept_its_message_is_replayable(store, trigger):
    run_id = _failed(
        trigger=trigger,
        agent_ids=["support-agent"],
        config={"agent_name": "support-agent", "new_message": MESSAGE},
    )

    assert run_main.get_run(run_id, owner_id=OWNER)["input_available"] is True
    [listed] = run_main.list_runs(owner_id=OWNER)
    assert listed["input_available"] is True


def test_an_agent_run_without_its_message_is_not_replayable(store):
    run_id = _failed(
        trigger="chat", agent_ids=["support-agent"], config={"agent_name": "support-agent"}
    )

    assert run_main.get_run(run_id, owner_id=OWNER)["input_available"] is False


def test_a_workflow_run_still_depends_on_its_file(store):
    with_file = _failed(
        trigger="workflow", agent_ids=["3"], config={}, file_bytes=b"a;1\n", file_name="in.csv"
    )
    without_file = _failed(trigger="workflow", agent_ids=["3"], config={})

    assert run_main.get_run(with_file, owner_id=OWNER)["input_available"] is True
    assert run_main.get_run(without_file, owner_id=OWNER)["input_available"] is False
    # The disk path stays private.
    assert "input_file_path" not in run_main.get_run(with_file, owner_id=OWNER)

"""Each run records which agent definition produced it (Dipankar's audit point).

``start_run`` pins the agents' fingerprints into ``config["_agent_versions"]``,
the same way a workflow run already pins its workflow version — so a stale,
multi-replica definition can be told apart after the fact. It is best-effort:
the audit must never break or stall a run. ``agent_fingerprint`` is stubbed here
so the test exercises the run-log wiring, not the fingerprint query itself.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from apowerb.core import agent_runtime_sync, run_main

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


def _stub_fingerprint(monkeypatch, mapping):
    """agent_fingerprint(id) -> mapping[id] (a tuple), or None if absent."""
    monkeypatch.setattr(
        agent_runtime_sync, "agent_fingerprint", lambda agent_id: mapping.get(agent_id)
    )


def test_an_agent_run_pins_its_fingerprint(store, monkeypatch):
    _stub_fingerprint(monkeypatch, {3: ((3, "2026-10-05 09:00:00.0", 42),)})
    # Chat stores the app name ("agent3"); it must resolve to agent 3.
    run_id = run_main.start_run(
        trigger="chat",
        owner_id=OWNER,
        agent_ids=["agent3"],
        config={"agent_name": "agent3"},
    )

    run = run_main.get_run(run_id, owner_id=OWNER)

    assert run["config"]["_agent_versions"] == {"3": [[3, "2026-10-05 09:00:00.0", 42]]}
    assert run["config"]["agent_name"] == "agent3"  # existing config preserved


def test_list_runs_exposes_the_pinned_version(store, monkeypatch):
    _stub_fingerprint(monkeypatch, {7: ((7, "2026-10-05 10:00:00.0", 5),)})
    run_main.start_run(trigger="schedule", owner_id=OWNER, agent_ids=[7])

    [listed] = run_main.list_runs(owner_id=OWNER)

    assert listed["config"]["_agent_versions"] == {
        "7": [[7, "2026-10-05 10:00:00.0", 5]]
    }


def test_a_workflow_run_is_untouched(store, monkeypatch):
    _stub_fingerprint(monkeypatch, {})
    run_id = run_main.start_run(
        trigger="webhook",
        owner_id=OWNER,
        agent_ids=[],
        config={"workflow_id": "wf1", "version": 2},
    )

    run = run_main.get_run(run_id, owner_id=OWNER)

    assert "_agent_versions" not in run["config"]
    assert run["config"]["version"] == 2


def test_a_missing_agent_pins_nothing_and_still_runs(store, monkeypatch):
    _stub_fingerprint(monkeypatch, {})  # fingerprint returns None for every id
    run_id = run_main.start_run(
        trigger="schedule", owner_id=OWNER, agent_ids=["agent9"]
    )

    run = run_main.get_run(run_id, owner_id=OWNER)

    assert run is not None  # the run is still recorded
    assert "_agent_versions" not in run["config"]


def test_a_fingerprint_failure_never_breaks_the_run(store, monkeypatch):
    def boom(agent_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(agent_runtime_sync, "agent_fingerprint", boom)
    run_id = run_main.start_run(trigger="schedule", owner_id=OWNER, agent_ids=[3])

    run = run_main.get_run(run_id, owner_id=OWNER)

    assert run is not None
    assert "_agent_versions" not in run["config"]


def test_a_replayed_config_does_not_inherit_stale_versions(store, monkeypatch):
    # A replay copies the original run's config; start_run must overwrite the
    # key with the fresh run's own agents, never keep the previous values.
    _stub_fingerprint(monkeypatch, {})
    run_id = run_main.start_run(
        trigger="schedule",
        owner_id=OWNER,
        agent_ids=[],
        config={"_agent_versions": {"3": [[3, "old", 1]]}},
    )

    run = run_main.get_run(run_id, owner_id=OWNER)

    assert "_agent_versions" not in run["config"]

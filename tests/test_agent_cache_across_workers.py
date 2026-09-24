"""An agent written through one worker must be served fresh by every worker.

``invalidate_agent_runtime`` only reaches the process that served the write:
each uvicorn worker, each replica, holds its own ``ApiServer.runner_dict`` and
``AgentLoader`` cache. Behind more than one of them, an edited agent kept
answering with its old definition wherever the edit did not land, a deleted
one kept running, and an agent created on one replica had no module on the
others until their next restart.

Two ``ApiServer`` instances built by ``get_fast_api_app(web=False)`` -- the
factory main.py uses -- stand for two workers, each with its own agents
directory. The database is a JSON file both of them read: the stubs on disk
are the canonical ones, and ``to_agent`` is replaced by a builder reading that
file. The fingerprint is checked against a real SQLite store separately.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from google.adk.agents import LlmAgent
from google.adk.cli.api_server import ApiServer
from google.adk.cli.fast_api import get_fast_api_app

import apowerb.core.agent_helpers as agent_helpers
from apowerb.core import adk_agent_builder, agent_main, agent_runtime_sync
from apowerb.schema.agent_schema import AgentCreateSchema

AGENT_ID = 93
APP_NAME = f"agent{AGENT_ID}"


class _Database:
    """The rows both workers read, with the version a write bumps."""

    def __init__(self, path: Path):
        self.path = path

    def write(self, model: str, version: int) -> None:
        self.path.write_text(json.dumps({"model": model, "version": version}))

    def read(self) -> dict | None:
        return json.loads(self.path.read_text()) if self.path.exists() else None

    def delete(self) -> None:
        self.path.unlink()

    def fingerprint(self, agent_id: int):
        row = self.read()
        return None if row is None else (agent_id, row["version"])

    def to_agent(self, agent_name: str) -> LlmAgent:
        return LlmAgent(name=agent_name, model=self.read()["model"])


def _worker(agents_dir: Path) -> ApiServer:
    captured: dict = {}
    original_init = ApiServer.__init__

    def capture(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        captured["server"] = self

    ApiServer.__init__ = capture
    try:
        get_fast_api_app(agents_dir=str(agents_dir), web=False)
    finally:
        ApiServer.__init__ = original_init
    return captured["server"]


def _without_sync(get_runner_async):
    """ADK's method as it is before main.py wraps it -- if main was imported."""
    while getattr(get_runner_async, "keeps_agents_in_sync", False):
        get_runner_async = get_runner_async.__wrapped__
    return get_runner_async


@pytest.fixture
def cluster(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ApiServer, "get_runner_async", _without_sync(ApiServer.get_runner_async)
    )
    db = _Database(tmp_path / "db.json")
    db.write("gemini-2.5-flash", version=1)
    monkeypatch.setattr(agent_helpers, "to_agent", db.to_agent)

    dirs = {name: tmp_path / name for name in ("a", "b")}
    for agents_dir in dirs.values():
        agents_dir.mkdir()
        adk_agent_builder.ensure_agent_module(AGENT_ID, str(agents_dir))

    workers = {name: _worker(agents_dir) for name, agents_dir in dirs.items()}
    yield SimpleNamespace(db=db, dirs=dirs, **workers)

    for worker in workers.values():
        worker.agent_loader.remove_agent_from_cache(APP_NAME)


@pytest.fixture
def synced(cluster, monkeypatch):
    """The patch main.py installs, reading the fake database."""
    monkeypatch.setattr(
        ApiServer,
        "get_runner_async",
        agent_runtime_sync.keep_runners_in_sync(
            ApiServer.get_runner_async, fingerprint=cluster.db.fingerprint
        ),
    )
    return cluster


def _edit_on_worker_a(cluster, model: str, version: int) -> None:
    """A PUT served by worker A: the row changes, A drops its own cache."""
    cluster.db.write(model, version)
    agent_runtime_sync.drop_cached_agent(cluster.a, APP_NAME)


def test_main_installs_the_sync():
    import apowerb.main  # noqa: F401 -- the import is what installs it

    assert getattr(ApiServer.get_runner_async, "keeps_agents_in_sync", False)


async def test_without_the_sync_the_other_worker_serves_the_stale_agent(cluster):
    """The defect, on ADK alone: what the sync exists to fix."""
    await cluster.a.get_runner_async(APP_NAME)
    await cluster.b.get_runner_async(APP_NAME)

    _edit_on_worker_a(cluster, "gemini-2.5-pro", version=2)

    assert (await cluster.a.get_runner_async(APP_NAME)).agent.model == "gemini-2.5-pro"
    assert (await cluster.b.get_runner_async(APP_NAME)).agent.model == "gemini-2.5-flash"


async def test_an_edit_on_one_worker_reaches_the_other(synced):
    await synced.a.get_runner_async(APP_NAME)
    before = await synced.b.get_runner_async(APP_NAME)
    assert before.agent.model == "gemini-2.5-flash"

    _edit_on_worker_a(synced, "gemini-2.5-pro", version=2)

    after = await synced.b.get_runner_async(APP_NAME)
    assert after.agent.model == "gemini-2.5-pro"


async def test_an_unchanged_agent_keeps_its_runner(synced):
    first = await synced.b.get_runner_async(APP_NAME)
    assert await synced.b.get_runner_async(APP_NAME) is first


async def test_a_delete_on_one_worker_stops_the_other(synced):
    await synced.b.get_runner_async(APP_NAME)

    synced.db.delete()

    with pytest.raises(HTTPException) as exc:
        await synced.b.get_runner_async(APP_NAME)
    assert exc.value.status_code == 404
    assert APP_NAME not in synced.b.runner_dict


async def test_an_agent_created_elsewhere_gets_its_module_here(synced):
    """Created on replica A after B booted: B has no stub on its own disk."""
    stub = synced.dirs["b"] / APP_NAME
    for f in stub.iterdir():
        f.unlink()
    stub.rmdir()

    runner = await synced.b.get_runner_async(APP_NAME)

    assert runner.agent.model == "gemini-2.5-flash"
    assert (stub / "agent.py").exists()


async def test_an_unreadable_database_keeps_serving_the_cached_runner(synced, monkeypatch):
    first = await synced.b.get_runner_async(APP_NAME)

    def unreachable(_agent_id):
        raise OSError("database unreachable")

    monkeypatch.setattr(
        ApiServer,
        "get_runner_async",
        agent_runtime_sync.keep_runners_in_sync(
            ApiServer.get_runner_async.__wrapped__, fingerprint=unreachable
        ),
    )
    assert await synced.b.get_runner_async(APP_NAME) is first


# --- the fingerprint, against the real store ---------------------------------

OWNER = "u@example.com"


@pytest.fixture
def store(monkeypatch):
    from tests.test_agent_revisions import _sqlite_engine

    engine = _sqlite_engine()
    agent_store = agent_main.agent_store
    monkeypatch.setattr(agent_store, "engine", engine)
    agent_store.metadata.create_all(engine)
    monkeypatch.setattr(agent_main, "create_agent_module", lambda **k: None)
    return agent_store


def _insert(store, agent_id: int, sub_agents: list[str]) -> None:
    with store.engine.begin() as conn:
        conn.execute(
            store.agent_table.insert().values(
                agent_id=agent_id,
                agent_name=f"a{agent_id}",
                agent_model="gemini-2.5-flash",
                agent_model_params=json.dumps({}),
                agent_instruction="v1",
                agent_tools=json.dumps([]),
                agent_type="llm",
                sub_agents=json.dumps(sub_agents),
                organization_id="example.com",
                project_id="p1",
                owner_id=OWNER,
                updated_at="2026-09-24 10:00:00",
            )
        )


def _update(agent_id: int, instruction: str) -> None:
    payload = AgentCreateSchema(
        agent_name=f"a{agent_id}",
        agent_model="gemini-2.5-flash",
        agent_description="d",
        agent_instruction=instruction,
        agent_type="llm",
        project_id="p1",
    ).model_copy(update={"owner_id": OWNER, "organization_id": "example.com"})
    agent_main.update_agent(agent_id, payload, user_id=OWNER)


def test_fingerprint_of_a_missing_agent_is_none(store):
    assert agent_runtime_sync.agent_fingerprint(404) is None


def test_fingerprint_moves_with_every_update_even_within_the_same_second(store):
    _insert(store, 1, [])
    fingerprints = [agent_runtime_sync.agent_fingerprint(1)]
    for instruction in ("v2", "v3"):
        _update(1, instruction)
        fingerprints.append(agent_runtime_sync.agent_fingerprint(1))
    assert len(set(fingerprints)) == 3


def test_fingerprint_moves_when_a_sub_agent_changes(store):
    """The parent's runner embeds its sub-agents: editing one must rebuild it."""
    _insert(store, 1, ["agent2"])
    _insert(store, 2, ["3"])
    _insert(store, 3, ["agent1"])  # a cycle must not loop
    before = agent_runtime_sync.agent_fingerprint(1)

    _update(3, "v2")

    assert agent_runtime_sync.agent_fingerprint(1) != before

"""``run_now`` laisse une planification : elle doit partir avec l'agent.

Réf. roadmap 99. ``POST /api/adk/run_now`` crée d'office une planification
(inactive) au nom de l'agent, faute de quoi th2etl n'a rien à exécuter. Rien ne
la supprimait ensuite : l'agent supprimé, elle restait dans l'ordonnanceur.

Banc : le vrai ``Th2etlAPIClient``, dont la session HTTP répond comme th2etl
(schedulers, triggers, run, DELETE) à partir d'un état en mémoire, et le vrai
``AgentStore`` sur SQLite. Ce qui est vérifié, c'est ce qui reste dans
l'ordonnanceur après la suppression de l'agent.
"""

from __future__ import annotations

import json
import re

import pytest
import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from apowerb.auth.dependencies import get_current_user
from apowerb.core import agent_main
from apowerb.scheduler import mage
from apowerb.scheduler import run_agent_background as background
from apowerb.scheduler.th2etl_client import Th2etlAPIClient

OWNER = "u@example.com"
BASE = "http://etl.test"


def _answer(status: int, payload=None) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response._content = b"" if payload is None else json.dumps(payload).encode()
    response.headers["Content-Type"] = "application/json"
    return response


class FakeTh2etl:
    """Les routes de th2etl qu'emprunte le client, sur un état en mémoire."""

    def __init__(self):
        self.schedulers: dict[str, dict] = {}
        self.triggers: dict[str, dict] = {}
        self.headers: dict[str, str] = {}

    def get(self, url, **_):
        path = url.removeprefix(BASE)
        if path == "/schedulers/":
            return _answer(200, list(self.schedulers.values()))
        if path == "/pipelines/":
            return _answer(200, [{"name": "agents"}])
        if path.startswith("/pipelines/"):
            return _answer(200, {"name": path.rsplit("/", 1)[-1]})
        return _answer(404, {"detail": "Not Found"})

    def post(self, url, json=None, **_):
        path = url.removeprefix(BASE)
        if path == "/triggers/":
            self.triggers[json["name"]] = json
            return _answer(201, json)
        if path == "/schedulers/":
            self.schedulers[json["name"]] = dict(json)
            return _answer(201, json)
        match = re.fullmatch(r"/schedulers/([^/]+)/run", path)
        if match and match.group(1) in self.schedulers:
            return _answer(200, {"run_id": 1, "status": "pending"})
        return _answer(404, {"detail": "Not Found"})

    def put(self, url, json=None, **_):
        match = re.fullmatch(r"/schedulers/([^/]+)", url.removeprefix(BASE))
        if match and match.group(1) in self.schedulers:
            self.schedulers[match.group(1)].update(json or {})
            return _answer(200, self.schedulers[match.group(1)])
        return _answer(404, {"detail": "Not Found"})

    def delete(self, url, **_):
        kind, _, name = url.removeprefix(BASE).strip("/").partition("/")
        table = {"schedulers": self.schedulers, "triggers": self.triggers}.get(kind)
        if table is None or table.pop(name, None) is None:
            return _answer(404, {"detail": f"{kind} not found"})
        return _answer(204)


def _sqlite_engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def _attach(dbapi_connection, _record):  # pragma: no cover
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS public")

    return engine


@pytest.fixture
def th2etl(monkeypatch):
    fake = FakeTh2etl()
    client = Th2etlAPIClient(BASE, timeout=1, api_key="k")
    client._http = fake
    orchestrator = mage.AgentOrchestrator(client=client)
    orchestrator._pipeline_initialized = True
    monkeypatch.setattr(mage, "_orchestrator_instance", orchestrator)
    monkeypatch.setattr(background, "get_orchestrator", lambda: orchestrator)
    monkeypatch.setattr(background, "create_agent_run_token", lambda **_: "jwt")
    return fake


@pytest.fixture
def agent(monkeypatch):
    engine = _sqlite_engine()
    store = agent_main.agent_store
    monkeypatch.setattr(store, "engine", engine)
    store.metadata.create_all(engine)
    monkeypatch.setattr(agent_main, "delete_agent_module", lambda **_: None)
    row = {
        "agent_id": 75,
        "agent_name": "a75",
        "agent_model": "gemini-2.5-flash",
        "agent_description": "d",
        "agent_instruction": "i",
        "agent_model_params": json.dumps({}),
        "agent_tools": json.dumps([]),
        "agent_type": "llm",
        "sub_agents": json.dumps([]),
        "organization_id": "example.com",
        "project_id": "p1",
        "owner_id": OWNER,
        "created_at": "2026-09-23 10:00:00",
        "updated_at": "2026-09-23 10:00:00",
        "status": "active",
    }
    with engine.begin() as conn:
        conn.execute(store.agent_table.insert().values(**row))
    return row


def _run_now(agent_id: str):
    import apowerb.routers.adk_runner as adk

    app = FastAPI()
    app.include_router(adk.router, prefix="/api/adk")
    app.dependency_overrides[get_current_user] = lambda: type(
        "U", (), {"email": OWNER}
    )()
    return TestClient(app).post(
        "/api/adk/run_now", json={"agent_id": agent_id, "message": "go"}
    )


@pytest.mark.parametrize("agent_id", ["75", "agent75"])
def test_the_schedule_run_now_creates_is_removed_with_the_agent(th2etl, agent, agent_id):
    response = _run_now(agent_id)
    assert response.status_code == 200, response.text
    # Le constat du ticket : run_now a bien laissé une planification.
    assert agent_id in th2etl.schedulers
    assert th2etl.schedulers[agent_id]["active"] is False

    agent_main.delete_agent("75", user_id=OWNER)

    assert th2etl.schedulers == {}, "planification orpheline après suppression"
    assert th2etl.triggers == {}


def test_deleting_an_agent_without_schedule_still_deletes_it(th2etl, agent):
    agent_main.delete_agent("75", user_id=OWNER)

    assert agent_main.get_agent_by_id("75", OWNER) is None


def test_an_unreachable_scheduler_does_not_block_the_agent_deletion(th2etl, agent, monkeypatch):
    def _down(*_a, **_k):
        raise requests.ConnectionError("th2etl down")

    monkeypatch.setattr(th2etl, "get", _down)

    agent_main.delete_agent("75", user_id=OWNER)

    assert agent_main.get_agent_by_id("75", OWNER) is None


def test_someone_elses_agent_keeps_its_schedule(th2etl, agent):
    assert _run_now("75").status_code == 200

    agent_main.delete_agent("75", user_id="intrus@example.com")

    assert "75" in th2etl.schedulers

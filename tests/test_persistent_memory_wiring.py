"""Le branchement de la mémoire : là où le test ADK ne peut pas regarder.

``to_agent`` lit Postgres, on ne l'exécute donc pas ici (même contrainte que
``tests/usage/usage_recorder/test_usage_recorder_wiring.py``) : on vérifie que
le callback est posé après tout autre ``after_agent_callback`` et avant la
construction de l'agent. Le reste est exercé pour de vrai : l'enregistrement
du schéma auprès d'ADK, et la route d'effacement de bout en bout.
"""

from __future__ import annotations

import inspect
import time
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.adk.events import Event
from google.adk.sessions import Session
from google.genai import types
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from apowerb.auth.dependencies import get_current_user
from apowerb.core.agent_helpers.agent_utils import to_agent
from apowerb.memory.service import (
    MEMORY_SERVICE_URI,
    PersistentMemoryService,
    register_memory_service,
)
from apowerb.routers import agent_memory


def test_to_agent_pose_le_callback_memoire_apres_les_autres_et_avant_l_agent():
    src = inspect.getsource(to_agent)
    derniere_pose = src.rindex('agent_kwargs["after_agent_callback"] = (')
    memoire = src.index("with_memory_callback(")
    construction = src.index("agent = LlmAgent(**agent_kwargs)")
    assert derniere_pose < memoire < construction


def test_adk_resout_notre_schema_en_notre_service():
    from google.adk.cli.service_registry import get_service_registry

    register_memory_service()
    service = get_service_registry().create_memory_service(MEMORY_SERVICE_URI)
    assert isinstance(service, PersistentMemoryService)


def test_l_application_branche_bien_ce_schema():
    main = (Path(__file__).resolve().parents[1] / "src/apowerb/main.py").read_text(encoding="utf-8")
    assert "register_memory_service()" in main
    assert "memory_service_uri=MEMORY_SERVICE_URI" in main


@pytest.fixture()
def service():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def factory():
        async with maker() as s:
            yield s

    return PersistentMemoryService(session_factory=factory, schema=None, retention_days=90)


def _session(app, user, text, sid):
    event = Event(
        id=f"e-{sid}", invocation_id="inv", author="user", timestamp=time.time(),
        content=types.Content(role="user", parts=[types.Part(text=text)]),
    )
    return Session(id=sid, app_name=app, user_id=user, events=[event])


def _client(service, email):
    app = FastAPI()
    app.include_router(agent_memory.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: type("U", (), {"email": email})()
    app.dependency_overrides[agent_memory._memory_service] = lambda: service
    return TestClient(app)


async def test_la_route_efface_ma_memoire_et_seulement_la_mienne(service):
    await service.add_session_to_memory(_session("agent1", "alice@ex.com", "note alice", "s1"))
    await service.add_session_to_memory(_session("agent1", "bob@ex.com", "note bob", "s2"))

    r = _client(service, "alice@ex.com").delete("/api/agents/1/memory")

    assert r.status_code == 204
    assert (await service.search_memory(app_name="agent1", user_id="alice@ex.com", query="note")).memories == []
    assert len((await service.search_memory(app_name="agent1", user_id="bob@ex.com", query="note")).memories) == 1


def test_un_identifiant_d_agent_hostile_est_refuse(service):
    assert _client(service, "alice@ex.com").delete("/api/agents/a.b/memory").status_code == 400

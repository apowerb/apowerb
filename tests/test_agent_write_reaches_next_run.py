"""An agent edited through the API must answer with its new definition on the
very next run -- no manual ``POST /agents/{id}/reload`` in between.

Réf. roadmap 93. Measured in dev on 2026-09-23: after a ``PUT /agents/{id}``
changing the model, the next ``run_now`` still ran on the old model and was
reported successful. Every run goes through ADK's ``/run``, which serves the
runner cached in ``ApiServer.runner_dict`` and the agent cached by
``AgentLoader``; the agent module is only a stub calling ``to_agent()`` at
import time, so rewriting it on disk changes nothing while those caches hold.

The server is built by ``get_fast_api_app(web=False)`` -- the factory main.py
uses -- so these tests drive the real ADK ``ApiServer`` and ``AgentLoader``.
The only fake is the persistence layer: the agent module reads its definition
from a JSON file, which the patched write functions rewrite -- the role the
database plays for ``to_agent()``.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from google.adk.cli.api_server import ApiServer
from google.adk.cli.fast_api import get_fast_api_app

from apowerb.auth.dependencies import get_current_user
from apowerb.routers import agents as agents_router

AGENT_ID = 93
APP_NAME = f"agent{AGENT_ID}"

_MODULE = '''
import json
from pathlib import Path

from google.adk.agents import LlmAgent

_cfg = json.loads((Path(__file__).parent / "definition.json").read_text())
root_agent = LlmAgent(
    name="{name}", model=_cfg["model"], instruction=_cfg["instruction"]
)
'''


def _write_definition(agent_dir: Path, model: str, instruction: str) -> None:
    (agent_dir / "definition.json").write_text(
        json.dumps({"model": model, "instruction": instruction})
    )


@pytest.fixture
def adk(tmp_path, monkeypatch):
    agent_dir = tmp_path / APP_NAME
    agent_dir.mkdir()
    (agent_dir / "__init__.py").write_text("from . import agent\n")
    (agent_dir / "agent.py").write_text(_MODULE.format(name=APP_NAME))
    _write_definition(agent_dir, "gemini-2.5-flash", "old instruction")

    captured: dict = {}
    original_init = ApiServer.__init__

    def capture(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        captured["server"] = self

    monkeypatch.setattr(ApiServer, "__init__", capture)
    app = get_fast_api_app(agents_dir=str(tmp_path), web=False)
    server = captured["server"]

    app.include_router(agents_router.router)
    app.state.adk_web_server = server
    app.state.adk_agent_loader = server.agent_loader
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        email="owner@example.com"
    )

    monkeypatch.setattr(agents_router, "validate_agent_model", lambda *a, **k: None)

    def fake_update(agent_id, agent, user_id):
        _write_definition(agent_dir, agent.agent_model, agent.agent_instruction)
        return {"agent_id": f"agent{agent_id}", "message": "Agent updated successfully."}

    def fake_delete(agent_id, user_id):
        shutil.rmtree(agent_dir)

    monkeypatch.setattr(agents_router, "update_agent_func", fake_update)
    monkeypatch.setattr(agents_router, "delete_agent", fake_delete)

    yield SimpleNamespace(app=app, server=server)

    server.agent_loader.remove_agent_from_cache(APP_NAME)


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_put_is_seen_by_the_next_run_without_reload(adk):
    before = await adk.server.get_runner_async(APP_NAME)
    assert before.agent.model == "gemini-2.5-flash"
    assert before.agent.instruction == "old instruction"

    async with _client(adk.app) as client:
        resp = await client.put(
            f"/agents/{APP_NAME}",
            json={
                "agent_name": "a93",
                "agent_model": "gemini-2.5-pro",
                "agent_instruction": "new instruction",
                "agent_description": "d",
                "agent_type": "llm",
            },
        )
    assert resp.status_code == 200, resp.text

    after = await adk.server.get_runner_async(APP_NAME)
    assert after.agent.model == "gemini-2.5-pro"
    assert after.agent.instruction == "new instruction"


async def test_delete_stops_serving_the_cached_agent(adk):
    await adk.server.get_runner_async(APP_NAME)

    async with _client(adk.app) as client:
        resp = await client.delete(f"/agents/{AGENT_ID}")
    assert resp.status_code == 200, resp.text

    with pytest.raises(HTTPException) as exc:
        await adk.server.get_runner_async(APP_NAME)
    assert exc.value.status_code == 404

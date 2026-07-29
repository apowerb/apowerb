"""Tests IDOR pour routers/adk_runner.py.

Vérifient qu'un utilisateur authentifié (user A) ne peut pas accéder
aux sessions ADK d'un autre utilisateur (user B) en manipulant `user_id`
dans le body ou le path.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


USER_A = "alice@example.com"
USER_B = "bob@example.com"


def _fake_user(email: str):
    u = MagicMock()
    u.email = email
    u.user_id = 1 if email == USER_A else 2
    u.role = "USER"
    return u


@pytest.fixture()
def client():
    """Build a TestClient where get_current_user returns USER_A."""
    from th2agent.routers.adk_runner import router
    from th2agent.auth.dependencies import get_current_user

    app = FastAPI()
    app.include_router(router, prefix="/api/adk")

    async def override_user_a():
        return _fake_user(USER_A)

    app.dependency_overrides[get_current_user] = override_user_a
    with patch("th2agent.routers.adk_runner.get_agent_folder_name", return_value="agent1"), \
         patch("th2agent.routers.adk_runner.run_adk_agent", new_callable=AsyncMock) as mock_run, \
         patch("th2agent.routers.adk_runner.get_adk_session", new_callable=AsyncMock) as mock_get, \
         patch("th2agent.routers.adk_runner.create_adk_agent_session", new_callable=AsyncMock) as mock_create, \
         patch("th2agent.routers.adk_runner.update_adk_agent_session", new_callable=AsyncMock) as mock_update, \
         patch("th2agent.routers.adk_runner.delete_adk_agent_session", new_callable=AsyncMock) as mock_delete, \
         patch("th2agent.routers.adk_runner.stream_adk_agent") as mock_stream:
        mock_run.return_value = {"status": "ok"}
        mock_get.return_value = {"events": []}
        mock_create.return_value = {"session_id": "s1"}
        mock_update.return_value = {"ok": True}
        mock_delete.return_value = {"ok": True}
        mock_stream.return_value = iter([])

        c = TestClient(app)
        c._mocks = {
            "run": mock_run,
            "get": mock_get,
            "create": mock_create,
            "update": mock_update,
            "delete": mock_delete,
        }
        yield c


class TestIdorRunEndpoint:
    def test_run_rejects_mismatched_user_id(self, client):
        resp = client.post(
            "/api/adk/run",
            json={
                "agent_name": "agent1",
                "user_id": USER_B,
                "session_id": "session_1",
                "new_message": {"role": "user", "parts": [{"text": "hi"}]},
            },
        )
        assert resp.status_code == 403, resp.text
        assert client._mocks["run"].await_count == 0

    def test_run_accepts_matching_user_id(self, client):
        resp = client.post(
            "/api/adk/run",
            json={
                "agent_name": "agent1",
                "user_id": USER_A,
                "session_id": "session_1",
                "new_message": {"role": "user", "parts": [{"text": "hi"}]},
            },
        )
        assert resp.status_code == 200, resp.text


class TestIdorRunSseEndpoint:
    def test_run_sse_rejects_mismatched_user_id(self, client):
        resp = client.post(
            "/api/adk/run_sse",
            json={
                "agent_name": "agent1",
                "user_id": USER_B,
                "session_id": "session_1",
                "new_message": {"role": "user", "parts": [{"text": "hi"}]},
            },
        )
        assert resp.status_code == 403, resp.text


class TestIdorSessionsEndpoints:
    def test_get_session_rejects_mismatched_user_id(self, client):
        resp = client.get(f"/api/adk/sessions/agent1/{USER_B}/session_1")
        assert resp.status_code == 403, resp.text
        assert client._mocks["get"].await_count == 0

    def test_get_session_trace_rejects_mismatched_user_id(self, client):
        resp = client.get(f"/api/adk/sessions/agent1/{USER_B}/session_1/trace")
        assert resp.status_code == 403, resp.text

    def test_update_session_rejects_mismatched_user_id(self, client):
        resp = client.patch(
            f"/api/adk/sessions/agent1/{USER_B}/session_1",
            json={
                "agent_name": "agent1",
                "user_id": USER_B,
                "session_id": "session_1",
                "data": {},
            },
        )
        assert resp.status_code == 403, resp.text

    def test_delete_session_rejects_mismatched_user_id(self, client):
        resp = client.delete(f"/api/adk/sessions/agent1/{USER_B}/session_1")
        assert resp.status_code == 403, resp.text
        assert client._mocks["delete"].await_count == 0

    def test_create_session_rejects_mismatched_user_id(self, client):
        resp = client.post(
            "/api/adk/sessions",
            json={
                "agent_name": "agent1",
                "user_id": USER_B,
                "session_id": "session_1",
                "data": {},
            },
        )
        assert resp.status_code == 403, resp.text


class TestIdorRunNowEndpoint:
    def test_run_now_rejects_explicit_mismatched_user_id(self, client):
        with patch("th2agent.scheduler.run_agent_background.get_agent_by_id") as mock_agent:
            mock_agent.return_value = {"agent_name": "agent1", "owner_id": USER_B}
            resp = client.post(
                "/api/adk/run_now",
                json={"agent_id": "agent1", "user_id": USER_B, "message": "hi"},
            )
            assert resp.status_code == 403, resp.text

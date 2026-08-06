"""py/stack-trace-exposure regression tests for routers/adk_runner.py.

Seventeen sites in this router used to forward ``str(exc)`` (or an
f-string embedding it) straight into the HTTP ``detail`` field. Exceptions
reaching this router come from the database, ADK, LLM providers and the
network, and their text can carry hosts, paths, connection strings or SQL
fragments.

Each test below forces the exact exception a given ``except`` branch
catches, using a distinctive sentinel string embedded in the exception
message, then asserts:
- the sentinel is absent from the response body (the leak is closed), and
- the original HTTP status code is preserved (the frontend depends on it).

These tests fail on ``origin/main`` (pre-fix) and pass on this branch.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

SENTINEL = "internal-adk-worker-host-10.0.0.42-leak"

USER_A = "alice@example.com"


def _fake_user():
    u = MagicMock()
    u.email = USER_A
    u.user_id = 1
    u.role = "USER"
    u.plan = "free"
    return u


@pytest.fixture()
def app_client():
    """TestClient with auth overridden and every ADK-facing call mocked to
    succeed by default. Individual tests override one mock to raise."""
    from apowerb.routers.adk_runner import router
    from apowerb.auth.dependencies import get_current_user

    app = FastAPI()
    app.include_router(router, prefix="/api/adk")

    async def override_user():
        return _fake_user()

    app.dependency_overrides[get_current_user] = override_user

    with (
        patch(
            "apowerb.routers.adk_runner.get_agent_folder_name", return_value="agent1"
        ),
        patch(
            "apowerb.routers.adk_runner.run_adk_agent", new_callable=AsyncMock
        ) as mock_run,
        patch(
            "apowerb.routers.adk_runner.get_adk_session", new_callable=AsyncMock
        ) as mock_get,
        patch(
            "apowerb.routers.adk_runner.create_adk_agent_session",
            new_callable=AsyncMock,
        ) as mock_create,
        patch(
            "apowerb.routers.adk_runner.update_adk_agent_session",
            new_callable=AsyncMock,
        ) as mock_update,
        patch(
            "apowerb.routers.adk_runner.delete_adk_agent_session",
            new_callable=AsyncMock,
        ) as mock_delete,
        patch("apowerb.routers.adk_runner.parse_session_to_trace") as mock_trace,
        patch("apowerb.routers.adk_runner.NewMessage") as mock_new_message,
    ):
        mock_run.return_value = {"status": "ok"}
        mock_get.return_value = {"events": []}
        mock_create.return_value = {"session_id": "s1"}
        mock_update.return_value = {"ok": True}
        mock_delete.return_value = {"ok": True}
        mock_trace.return_value = {"trace": []}
        mock_new_message.return_value = MagicMock()

        c = TestClient(app)
        c._mocks = {
            "run": mock_run,
            "get": mock_get,
            "create": mock_create,
            "update": mock_update,
            "delete": mock_delete,
            "trace": mock_trace,
        }
        yield c


def _run_payload(**overrides):
    payload = {
        "agent_name": "agent1",
        "user_id": USER_A,
        "session_id": "session_1",
        "new_message": {"role": "user", "parts": [{"text": "hi"}]},
    }
    payload.update(overrides)
    return payload


class TestRunAgentEndpoint:
    """POST /run — sites: validate_message, resolve_agent, ensure_session,
    run (ValueError), connection_error, timeout, unexpected_error."""

    def test_invalid_message_format_hides_exception_text(self, app_client):
        with patch(
            "apowerb.routers.adk_runner.NewMessage", side_effect=RuntimeError(SENTINEL)
        ):
            resp = app_client.post("/api/adk/run", json=_run_payload())
        assert resp.status_code == 400
        assert SENTINEL not in resp.text

    def test_resolve_agent_failure_hides_exception_text(self, app_client):
        with patch(
            "apowerb.routers.adk_runner.get_agent_folder_name",
            side_effect=RuntimeError(SENTINEL),
        ):
            resp = app_client.post("/api/adk/run", json=_run_payload())
        assert resp.status_code == 500
        assert SENTINEL not in resp.text

    def test_ensure_session_failure_hides_exception_text(self, app_client):
        app_client._mocks["get"].side_effect = RuntimeError("session missing")
        app_client._mocks["create"].side_effect = RuntimeError(SENTINEL)
        resp = app_client.post("/api/adk/run", json=_run_payload())
        assert resp.status_code == 500
        assert SENTINEL not in resp.text

    def test_run_value_error_hides_exception_text(self, app_client):
        app_client._mocks["run"].side_effect = ValueError(SENTINEL)
        resp = app_client.post("/api/adk/run", json=_run_payload())
        assert resp.status_code == 400
        assert SENTINEL not in resp.text

    def test_run_connection_error_hides_exception_text(self, app_client):
        app_client._mocks["run"].side_effect = ConnectionError(SENTINEL)
        resp = app_client.post("/api/adk/run", json=_run_payload())
        assert resp.status_code == 503
        assert SENTINEL not in resp.text

    def test_run_timeout_error_hides_exception_text(self, app_client):
        app_client._mocks["run"].side_effect = TimeoutError(SENTINEL)
        resp = app_client.post("/api/adk/run", json=_run_payload())
        assert resp.status_code == 504
        assert SENTINEL not in resp.text

    def test_run_unexpected_error_hides_exception_text(self, app_client):
        app_client._mocks["run"].side_effect = RuntimeError(SENTINEL)
        resp = app_client.post("/api/adk/run", json=_run_payload())
        assert resp.status_code == 500
        assert SENTINEL not in resp.text


class TestSessionEndpoints:
    """POST /sessions, GET .../session, GET .../trace, PATCH .../session,
    DELETE .../session — all wrap arbitrary backend exceptions in a bare
    ``str(e)``."""

    def test_create_session_failure_hides_exception_text(self, app_client):
        app_client._mocks["create"].side_effect = RuntimeError(SENTINEL)
        resp = app_client.post(
            "/api/adk/sessions",
            json={
                "agent_name": "agent1",
                "user_id": USER_A,
                "session_id": "session_1",
                "data": {},
            },
        )
        assert resp.status_code == 500
        assert SENTINEL not in resp.text

    def test_get_session_history_failure_hides_exception_text(self, app_client):
        app_client._mocks["get"].side_effect = RuntimeError(SENTINEL)
        resp = app_client.get(f"/api/adk/sessions/agent1/{USER_A}/session_1")
        assert resp.status_code == 500
        assert SENTINEL not in resp.text

    def test_get_session_trace_failure_hides_exception_text(self, app_client):
        app_client._mocks["get"].side_effect = RuntimeError(SENTINEL)
        resp = app_client.get(f"/api/adk/sessions/agent1/{USER_A}/session_1/trace")
        assert resp.status_code == 500
        assert SENTINEL not in resp.text

    def test_update_session_failure_hides_exception_text(self, app_client):
        app_client._mocks["update"].side_effect = RuntimeError(SENTINEL)
        resp = app_client.patch(
            f"/api/adk/sessions/agent1/{USER_A}/session_1",
            json={
                "agent_name": "agent1",
                "user_id": USER_A,
                "session_id": "session_1",
                "data": {},
            },
        )
        assert resp.status_code == 500
        assert SENTINEL not in resp.text

    def test_delete_session_failure_hides_exception_text(self, app_client):
        app_client._mocks["delete"].side_effect = RuntimeError(SENTINEL)
        resp = app_client.delete(f"/api/adk/sessions/agent1/{USER_A}/session_1")
        assert resp.status_code == 500
        assert SENTINEL not in resp.text


class TestRunFromJwtEndpoint:
    """POST /run_from_jwt — ValueError branch (400)."""

    def test_run_from_jwt_value_error_hides_exception_text(self):
        from apowerb.routers.adk_runner import router
        from apowerb.auth.dependencies import get_current_user

        app = FastAPI()
        app.include_router(router, prefix="/api/adk")

        async def override_user():
            return _fake_user()

        app.dependency_overrides[get_current_user] = override_user

        with patch(
            "apowerb.routers.adk_runner.run_agent_from_refresh_token",
            new_callable=AsyncMock,
            side_effect=ValueError(SENTINEL),
        ):
            client = TestClient(app)
            resp = client.post(
                "/api/adk/run_from_jwt",
                headers={"Authorization": "Bearer some-jwt"},
                json={"agent_id": "agent1"},
            )
        assert resp.status_code == 400
        assert SENTINEL not in resp.text


class TestScheduleRunEndpoint:
    """POST /schedule_run — generic Exception branch (500)."""

    def test_schedule_run_failure_hides_exception_text(self):
        from apowerb.routers.adk_runner import router
        from apowerb.auth.dependencies import get_current_user

        app = FastAPI()
        app.include_router(router, prefix="/api/adk")

        async def override_user():
            return _fake_user()

        app.dependency_overrides[get_current_user] = override_user

        with patch(
            "apowerb.routers.adk_runner.schedule_agent_run",
            new_callable=AsyncMock,
            side_effect=RuntimeError(SENTINEL),
        ):
            client = TestClient(app)
            resp = client.post(
                "/api/adk/schedule_run",
                json={
                    "agent_id": "agent1",
                    "user_id": USER_A,
                    "session_id": "session_1",
                    "new_message": {"role": "user", "parts": [{"text": "hi"}]},
                },
            )
        assert resp.status_code == 500
        assert SENTINEL not in resp.text


class TestRunNowEndpoint:
    """POST /run_now — generic Exception branch (500)."""

    def test_run_now_failure_hides_exception_text(self):
        from apowerb.routers.adk_runner import router
        from apowerb.auth.dependencies import get_current_user

        app = FastAPI()
        app.include_router(router, prefix="/api/adk")

        async def override_user():
            return _fake_user()

        app.dependency_overrides[get_current_user] = override_user

        with patch(
            "apowerb.scheduler.run_agent_background.get_agent_by_id",
            side_effect=RuntimeError(SENTINEL),
        ):
            client = TestClient(app)
            resp = client.post(
                "/api/adk/run_now",
                json={"agent_id": "agent1", "user_id": USER_A, "message": "hi"},
            )
        assert resp.status_code == 500
        assert SENTINEL not in resp.text


class TestRunFromRefreshTokenEndpoint:
    """POST /run_from_refresh_token — ValueError branch (400) and generic
    Exception branch (500)."""

    def _client(self):
        from apowerb.routers.adk_runner import router
        from apowerb.auth.dependencies import get_current_user

        app = FastAPI()
        app.include_router(router, prefix="/api/adk")

        async def override_user():
            return _fake_user()

        app.dependency_overrides[get_current_user] = override_user
        return TestClient(app)

    def test_run_from_refresh_token_value_error_hides_exception_text(self):
        client = self._client()
        with patch(
            "apowerb.scheduler.run_agent_background.run_agent_from_refresh_token",
            new_callable=AsyncMock,
            side_effect=ValueError(SENTINEL),
        ):
            resp = client.post(
                "/api/adk/run_from_refresh_token",
                headers={"Authorization": "Bearer some-refresh-token"},
            )
        assert resp.status_code == 400
        assert SENTINEL not in resp.text

    def test_run_from_refresh_token_unexpected_error_hides_exception_text(self):
        client = self._client()
        with patch(
            "apowerb.scheduler.run_agent_background.run_agent_from_refresh_token",
            new_callable=AsyncMock,
            side_effect=RuntimeError(SENTINEL),
        ):
            resp = client.post(
                "/api/adk/run_from_refresh_token",
                headers={"Authorization": "Bearer some-refresh-token"},
            )
        assert resp.status_code == 500
        assert SENTINEL not in resp.text

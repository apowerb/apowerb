"""py/stack-trace-exposure regression test for routers/adk_runner.py.

POST /run_sse used to catch any exception raised while starting the SSE
stream and re-raise it as ``HTTPException(status_code=500,
detail=str(e))`` — forwarding the exception text (which can come from a
pydantic validation error or a deeper library) straight to the client.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

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
def client():
    from apowerb.auth.dependencies import get_current_user
    from apowerb.routers.adk_runner import router

    app = FastAPI()
    app.include_router(router, prefix="/api/adk")

    async def override_user():
        return _fake_user()

    app.dependency_overrides[get_current_user] = override_user

    with patch(
        "apowerb.routers.adk_runner.get_agent_folder_name", return_value="agent1"
    ), patch(
        "apowerb.routers.adk_runner.NewMessage", side_effect=RuntimeError(SENTINEL)
    ):
        yield TestClient(app)


def test_run_sse_start_failure_hides_exception_text(client):
    resp = client.post(
        "/api/adk/run_sse",
        json={
            "agent_name": "agent1",
            "user_id": USER_A,
            "session_id": "session_1",
            "new_message": {"role": "user", "parts": [{"text": "hi"}]},
        },
    )

    assert resp.status_code == 500
    assert SENTINEL not in resp.text
    assert resp.json()["detail"]

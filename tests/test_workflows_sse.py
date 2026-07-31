"""Tests for B18 — /api/workflows/run-sse + /api/workflows/{wid}/cancel.

Two layers of tests:

* The **cancel endpoint** is a normal JSON endpoint and is exercised via the
  sync ``TestClient``. We pre-populate the in-memory registry to simulate a
  live run without actually holding an open SSE stream.

* The **run-sse endpoint** produces an ASGI StreamingResponse. Both
  ``TestClient`` and ``httpx.ASGITransport`` fully buffer the response body
  before yielding control to the caller, so we instead drive the endpoint
  function directly and drain its streaming body, asserting on the
  registration/cleanup side effects along the way.
"""

import asyncio
import json
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient


USER_EMAIL = "alice@example.com"


def _fake_user(email: str = USER_EMAIL):
    u = MagicMock()
    u.email = email
    u.user_id = 1
    u.role = "USER"
    return u


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


async def _blocking_runner(wid, cancel_event, canvas_agent_ids, file_bytes):
    """Emit one event then wait for cancel (keeps the stream alive)."""
    yield f"data: {json.dumps({'event': 'started', 'wid': wid})}\n\n"
    try:
        await asyncio.wait_for(cancel_event.wait(), timeout=2.0)
        yield f"data: {json.dumps({'event': 'cancelled'})}\n\n"
    except asyncio.TimeoutError:
        yield f"data: {json.dumps({'event': 'done'})}\n\n"


@pytest.fixture()
def workflows_app():
    """Mount the workflows router with an authenticated user override."""
    from apowerb.routers import workflows as workflows_module
    from apowerb.auth.dependencies import get_current_user

    workflows_module._runs.clear()
    workflows_module._workflow_runner = _blocking_runner

    app = FastAPI()
    app.include_router(workflows_module.router, prefix="/api")

    async def _user_override():
        return _fake_user()

    app.dependency_overrides[get_current_user] = _user_override

    return app, workflows_module


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_run_sse_without_auth_returns_401():
    from apowerb.routers import workflows as workflows_module
    from apowerb.auth.dependencies import get_current_user

    workflows_module._runs.clear()

    app = FastAPI()
    app.include_router(workflows_module.router, prefix="/api")

    async def _deny():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )

    app.dependency_overrides[get_current_user] = _deny

    client = TestClient(app)
    resp = client.post(
        "/api/workflows/run-sse",
        data={
            "canvas_agent_ids": json.dumps(["agent1"]),
            "workflow_id": "wf_unauth",
        },
    )
    assert resp.status_code == 401, resp.text


def test_cancel_without_auth_returns_401():
    from apowerb.routers import workflows as workflows_module
    from apowerb.auth.dependencies import get_current_user

    workflows_module._runs.clear()

    app = FastAPI()
    app.include_router(workflows_module.router, prefix="/api")

    async def _deny():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )

    app.dependency_overrides[get_current_user] = _deny

    client = TestClient(app)
    resp = client.post("/api/workflows/whatever/cancel")
    assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# Run-SSE endpoint (direct function drive)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_sse_with_auth_starts_stream_and_registers_wid(workflows_app):
    """Drive ``run_workflow_sse`` directly: it must register the wid in the
    in-memory registry and expose a StreamingResponse whose body iterates
    over the runner events."""
    _, workflows_module = workflows_app

    user = _fake_user()
    response = await workflows_module.run_workflow_sse(
        canvas_agent_ids=json.dumps(["agent1"]),
        workflow_id="wf_abc",
        config_json=None,
        file=None,
        current_user=user,
    )
    # StreamingResponse
    assert response.status_code == 200
    assert response.headers["X-Workflow-Id"] == "wf_abc"
    assert "wf_abc" in workflows_module._runs

    # Emit the first event and assert content, then cancel to release the
    # runner (otherwise the generator would wait for its 2-second timeout).
    body_iter = response.body_iterator
    first = await body_iter.__anext__()
    assert b"run_started" in first

    workflows_module._runs["wf_abc"]["cancel_event"].set()

    # Drain remaining events and verify the registry is cleaned up.
    async for _ in body_iter:
        pass
    assert "wf_abc" not in workflows_module._runs


@pytest.mark.asyncio
async def test_run_sse_auto_generates_wid_when_missing(workflows_app):
    _, workflows_module = workflows_app

    user = _fake_user()
    response = await workflows_module.run_workflow_sse(
        canvas_agent_ids=json.dumps(["agent1"]),
        workflow_id=None,
        config_json=None,
        file=None,
        current_user=user,
    )
    auto_wid = response.headers["X-Workflow-Id"]
    assert auto_wid, "server must auto-generate a wid when missing"
    assert auto_wid in workflows_module._runs

    # Release the runner then drain.
    workflows_module._runs[auto_wid]["cancel_event"].set()
    async for _ in response.body_iterator:
        pass


# ---------------------------------------------------------------------------
# Cancel endpoint (sync HTTP)
# ---------------------------------------------------------------------------


def test_cancel_unknown_wid_returns_404(workflows_app):
    app, _ = workflows_app
    client = TestClient(app)
    resp = client.post("/api/workflows/does-not-exist/cancel")
    assert resp.status_code == 404, resp.text


def test_cancel_valid_wid_returns_204_and_sets_event(workflows_app):
    app, workflows_module = workflows_app

    # Pre-populate the registry as if a run were live.
    ev = asyncio.Event()
    workflows_module._runs["wf_live"] = {
        "cancel_event": ev,
        "owner": USER_EMAIL,
        "task": None,
    }

    client = TestClient(app)
    resp = client.post("/api/workflows/wf_live/cancel")
    assert resp.status_code == 204, resp.text
    assert ev.is_set()


def test_cancel_only_affects_target_run(workflows_app):
    """Two live runs; cancelling A must not flip B's cancel_event."""
    app, workflows_module = workflows_app

    ev_a = asyncio.Event()
    ev_b = asyncio.Event()
    workflows_module._runs["wf_A"] = {
        "cancel_event": ev_a,
        "owner": USER_EMAIL,
        "task": None,
    }
    workflows_module._runs["wf_B"] = {
        "cancel_event": ev_b,
        "owner": USER_EMAIL,
        "task": None,
    }

    client = TestClient(app)
    resp = client.post("/api/workflows/wf_A/cancel")
    assert resp.status_code == 204, resp.text

    assert ev_a.is_set()
    assert not ev_b.is_set(), "cancelling A must not flip B's cancel_event"


def test_cancel_rejects_non_owner(workflows_app):
    """Someone else's run cannot be cancelled."""
    app, workflows_module = workflows_app

    ev = asyncio.Event()
    workflows_module._runs["wf_mallory"] = {
        "cancel_event": ev,
        "owner": "mallory@example.com",  # not USER_EMAIL
        "task": None,
    }

    client = TestClient(app)
    resp = client.post("/api/workflows/wf_mallory/cancel")
    assert resp.status_code == 403, resp.text
    assert not ev.is_set()

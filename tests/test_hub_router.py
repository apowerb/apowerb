"""Tests d'intégration pour routers/hub.py.

Vérifient :
- GET /api/hub retourne la liste des agents publiés (mocked backend).
- POST /api/hub/publish sans auth → 401.
- POST /api/hub/publish appelle publish_agent avec l'owner = current_user.email.
- POST /api/hub/clone clone pour le current user (le user_id propagé n'est pas cross-tenant).
- Clone d'un hub inexistant → renvoie un objet { "error": ... }.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient


USER_A_EMAIL = "alice@example.com"


def _fake_user(email: str = USER_A_EMAIL, user_id: int = 1):
    u = MagicMock()
    u.email = email
    u.user_id = user_id
    u.role = "USER"
    return u


def _build_app(*, user_email: str | None = USER_A_EMAIL):
    from apowerb.auth.dependencies import get_current_user
    from apowerb.routers.hub import router

    app = FastAPI()
    app.include_router(router, prefix="/api")

    async def _user_override():
        if user_email is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated"
            )
        return _fake_user(user_email)

    app.dependency_overrides[get_current_user] = _user_override
    return app


# ---------------------------------------------------------------------------
# 1. GET /api/hub — list published agents
# ---------------------------------------------------------------------------


class TestListHub:
    def test_list_hub_returns_published_agents(self):
        published = [
            {"hub_id": "hub1", "hub_name": "A1", "publisher_id": "someone@x.com"},
            {"hub_id": "hub2", "hub_name": "A2", "publisher_id": "other@y.com"},
        ]
        app = _build_app()
        client = TestClient(app)

        with patch(
            "apowerb.routers.hub.list_hub_agents",
            return_value=published,
        ) as mock_list:
            resp = client.get("/api/hub")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body) == 2
        assert body[0]["hub_id"] == "hub1"
        assert mock_list.called


# ---------------------------------------------------------------------------
# 2. POST /api/hub/publish — auth required
# ---------------------------------------------------------------------------


class TestPublishAuthRequired:
    def test_publish_without_auth_returns_401(self):
        app = _build_app(user_email=None)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/api/hub/publish",
            json={
                "agent_id": "agent1",
                "hub_name": "Alice agent",
                "hub_description": "desc",
            },
        )
        assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# 3. POST /api/hub/publish — propagates current_user.email as owner
# ---------------------------------------------------------------------------


class TestPublishScopesToCurrentUser:
    def test_publish_uses_current_user_email_as_publisher(self):
        app = _build_app()
        client = TestClient(app)

        with patch(
            "apowerb.routers.hub.publish_agent",
            return_value={"hub_id": "hub42", "hub_name": "Alice agent"},
        ) as mock_publish:
            resp = client.post(
                "/api/hub/publish",
                json={
                    "agent_id": "agent1",
                    "hub_name": "Alice agent",
                    "hub_description": "desc",
                    "hub_tags": ["bi"],
                    "hub_category": "analytics",
                },
            )

        assert resp.status_code == 200, resp.text
        # Inspect the kwargs to prove the router forwards the authenticated
        # user's email (and not any attacker-supplied value).
        kwargs = mock_publish.call_args.kwargs
        assert kwargs["user_id"] == USER_A_EMAIL
        assert kwargs["org_id"] == "example.com"  # domain part of the email


# ---------------------------------------------------------------------------
# 4. POST /api/hub/clone — clone happens for current user (not cross-tenant)
# ---------------------------------------------------------------------------


class TestCloneScopesToCurrentUser:
    def test_clone_forwards_current_user_as_owner(self):
        app = _build_app()
        client = TestClient(app)

        with patch(
            "apowerb.routers.hub.clone_hub_agent",
            return_value={
                "agent_id": "agent99",
                "cloned_from": "hub1",
                "message": "Agent cloned successfully.",
            },
        ) as mock_clone:
            resp = client.post(
                "/api/hub/clone",
                json={
                    "hub_agent_id": "hub1",
                    "clone_name": "MyCopy",
                },
            )

        assert resp.status_code == 200, resp.text
        # First positional argument is the hub id
        args, kwargs = mock_clone.call_args
        assert args[0] == "hub1"
        assert kwargs["user_id"] == USER_A_EMAIL
        assert kwargs["org_id"] == "example.com"
        assert kwargs["clone_name"] == "MyCopy"


# ---------------------------------------------------------------------------
# 5. Clone of a non-existent hub → 404
# ---------------------------------------------------------------------------


class TestCloneNonExistentHub:
    def test_clone_nonexistent_hub_returns_404(self):
        """Clone on a non-existent hub agent returns 404 (core raises
        HTTPException, router lets it propagate)."""
        app = _build_app()
        client = TestClient(app, raise_server_exceptions=False)

        # Simulate: the hub row does not exist -> get_hub_agent returns None
        # -> core must raise HTTPException(404).  No mock on clone_hub_agent.
        with patch(
            "apowerb.core.hub_main.get_hub_agent",
            return_value=None,
        ):
            resp = client.post(
                "/api/hub/clone",
                json={"hub_agent_id": "hub9999"},
            )

        assert resp.status_code == 404, resp.text
        assert "not found" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# 6. Delete cross-owner / non-existent → 403 / 404 (core-level)
# ---------------------------------------------------------------------------


class TestDeleteAuthorization:
    def test_delete_nonexistent_hub_returns_404(self):
        app = _build_app()
        client = TestClient(app, raise_server_exceptions=False)

        with patch(
            "apowerb.core.hub_main.get_hub_agent",
            return_value=None,
        ):
            resp = client.delete("/api/hub/hub9999")

        assert resp.status_code == 404, resp.text

    def test_delete_cross_owner_hub_returns_403(self):
        """Alice tries to delete Bob's hub agent -> 403 (not 404)."""
        app = _build_app()  # alice@example.com
        client = TestClient(app, raise_server_exceptions=False)

        bob_hub = {
            "hub_id": "hub1",
            "publisher_id": "bob@example.com",
            "hub_name": "Bob's agent",
        }
        with patch(
            "apowerb.core.hub_main.get_hub_agent",
            return_value=bob_hub,
        ):
            resp = client.delete("/api/hub/hub1")

        assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# 7. GET non-existent hub → 404
# ---------------------------------------------------------------------------


class TestGetHubNonExistent:
    def test_get_nonexistent_hub_returns_404(self):
        app = _build_app()
        client = TestClient(app, raise_server_exceptions=False)

        with patch(
            "apowerb.routers.hub.get_hub_agent",
            return_value=None,
        ):
            resp = client.get("/api/hub/hub9999")

        assert resp.status_code == 404, resp.text

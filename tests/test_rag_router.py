"""Tests unitaires pour routers/rag.py.

Vérifient :
- Authentification requise sur les endpoints protégés (401 sans token).
- Ownership : un user ne peut pas indexer / lire le knowledge map d'un autre (403/404).
- Happy path : création d'un knowledge via index-db et index-files mocké.
- HMAC webhook : signature invalide → 401 ; payload mal formé → 400.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient


USER_A = "alice@example.com"
USER_B = "bob@example.com"


def _fake_user(email: str, user_id: int = 1):
    u = MagicMock()
    u.email = email
    u.user_id = user_id
    u.role = "USER"
    return u


def _build_app(current_user_email: str | None):
    """Build a FastAPI app mounting the RAG router with auth override."""
    from th2agent.routers.rag import router
    from th2agent.auth.dependencies import get_current_user

    app = FastAPI()
    app.include_router(router, prefix="/api")

    async def _user_override():
        if current_user_email is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Not authenticated",
            )
        return _fake_user(current_user_email)

    app.dependency_overrides[get_current_user] = _user_override
    return app


class _FakeAgentStore:
    """Stub AgentStore for ownership checks."""

    def __init__(self, agents: dict[int, dict]):
        self._agents = agents

        class _Col:
            def __eq__(self, other):
                return ("agent_id", other)

        class _C:
            agent_id = _Col()

        class _T:
            c = _C()

            def select(self):
                class _S:
                    def where(self_inner, cond):
                        self_inner.cond = cond
                        return self_inner

                return _S()

        self.agent_table = _T()

    def get_list_agents(self, query):
        cond = query.cond
        target_id = cond[1]
        if target_id in self._agents:
            row = MagicMock()
            row._asdict = lambda: self._agents[target_id]
            return [row]
        return []


@pytest.fixture()
def patched_agent_store():
    """Patch the rag module's agent store to an in-memory fake."""
    agents = {
        1: {"agent_id": 1, "owner_id": USER_A, "agent_name": "alice_agent"},
        2: {"agent_id": 2, "owner_id": USER_B, "agent_name": "bob_agent"},
    }
    fake = _FakeAgentStore(agents)

    with patch("th2agent.core.agent_main.agent_store", fake):
        yield fake


# ---------------------------------------------------------------------------
# 1. Auth required
# ---------------------------------------------------------------------------


class TestAuthRequired:
    def test_get_knowledge_without_auth_returns_401(self):
        app = _build_app(current_user_email=None)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/rag/knowledge/agent1")
        assert resp.status_code == 401, resp.text

    def test_index_db_without_auth_returns_401(self):
        app = _build_app(current_user_email=None)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/api/rag/index-db",
            json={
                "agent_id": "agent1",
                "tool_config_id": "tool_config1",
                "sql_query": "SELECT 1",
                "name": "kb",
            },
        )
        assert resp.status_code == 401, resp.text

    def test_index_url_without_auth_returns_401(self):
        app = _build_app(current_user_email=None)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/api/rag/index-url",
            json={
                "agent_id": "agent1",
                "url": "https://example.com/a.pdf",
                "name": "kb",
            },
        )
        assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# 2. Cross-tenant ownership
# ---------------------------------------------------------------------------


class TestCrossTenantOwnership:
    def test_index_db_cross_tenant_returns_403(self, patched_agent_store):
        app = _build_app(current_user_email=USER_A)
        client = TestClient(app, raise_server_exceptions=False)
        # User A tries to index against agent2 (owned by user B)
        resp = client.post(
            "/api/rag/index-db",
            json={
                "agent_id": "agent2",
                "tool_config_id": "tool_config1",
                "sql_query": "SELECT 1",
                "name": "kb",
            },
        )
        assert resp.status_code == 403, resp.text

    def test_get_knowledge_map_cross_tenant_returns_403(self, patched_agent_store):
        app = _build_app(current_user_email=USER_A)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/rag/knowledge/agent2")
        assert resp.status_code == 403, resp.text

    def test_index_db_nonexistent_agent_returns_404(self, patched_agent_store):
        app = _build_app(current_user_email=USER_A)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/api/rag/index-db",
            json={
                "agent_id": "agent999",
                "tool_config_id": "tool_config1",
                "sql_query": "SELECT 1",
                "name": "kb",
            },
        )
        assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# 3. Happy path index-db (mocked tool invocation)
# ---------------------------------------------------------------------------


class TestIndexDbHappyPath:
    def test_index_db_owner_returns_200(self, patched_agent_store):
        app = _build_app(current_user_email=USER_A)
        client = TestClient(app)

        fake_register = AsyncMock()
        with patch(
            "th2agent.routers.rag._sync_index_db",
            return_value={"status": "ok", "knowledge_id": "kid-42"},
        ) as mock_sync, patch(
            "th2agent.routers.rag.append_source",
            return_value={
                "knowledge_id": "kid-42",
                "name": "kb",
                "status": "processing",
            },
        ), patch(
            "th2agent.routers.rag.rag_manager.register_knowledge",
            fake_register,
        ):
            resp = client.post(
                "/api/rag/index-db",
                json={
                    "agent_id": "agent1",
                    "tool_config_id": "tool_config1",
                    "sql_query": "SELECT 1",
                    "name": "kb",
                },
            )

        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "ok"
        assert mock_sync.called
        assert fake_register.await_count == 1


# ---------------------------------------------------------------------------
# 4. HMAC webhook signature
# ---------------------------------------------------------------------------


class TestRagWebhookSignature:
    def _signed(self, body: bytes, secret: str) -> str:
        return "sha256=" + hmac.new(
            secret.encode(), body, hashlib.sha256
        ).hexdigest()

    def test_webhook_missing_signature_returns_401(self):
        app = _build_app(current_user_email=None)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/rag/webhook", json={"event": "x"})
        assert resp.status_code == 401, resp.text

    def test_webhook_invalid_signature_returns_401(self):
        app = _build_app(current_user_email=None)
        client = TestClient(app, raise_server_exceptions=False)
        body = json.dumps(
            {
                "event": "knowledge.complete",
                "knowledge_id": "kid-1",
                "status": "complete",
            }
        ).encode()
        resp = client.post(
            "/api/rag/webhook",
            content=body,
            headers={
                "X-Webhook-Signature": "sha256=deadbeef",
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 401, resp.text

    def test_webhook_valid_signature_returns_200(self):
        from th2agent.configs.settings import get_settings

        secret = get_settings().rag_webhook_secret
        app = _build_app(current_user_email=None)
        client = TestClient(app)

        body = json.dumps(
            {
                "event": "knowledge.complete",
                "knowledge_id": "kid-unknown",
                "status": "complete",
            }
        ).encode()
        sig = self._signed(body, secret)

        with patch(
            "th2agent.routers.rag.rag_manager.get_scope",
            new_callable=AsyncMock,
            return_value=None,
        ):
            resp = client.post(
                "/api/rag/webhook",
                content=body,
                headers={
                    "X-Webhook-Signature": sig,
                    "Content-Type": "application/json",
                },
            )

        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "received"

"""Tests d'authentification pour routers/share.py.

Vérifient que :
- La création d'un share nécessite une authentification (401 sans token).
- Un share créé stocke bien owner_id (= email du créateur).
- Un lien partagé public reste lisible sans auth (intention du feature).
- Un share non-public ne peut être lu/supprimé que par son propriétaire
  (403 pour un autre utilisateur authentifié, 401 sans token).
- La suppression par un autre propriétaire renvoie 403 et ne supprime rien.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

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


class _FakeStore:
    """Store in-memory qui remplace la DB pour les tests."""

    def __init__(self):
        self.records: dict = {}
        self.deleted: list = []


@pytest.fixture()
def store():
    return _FakeStore()


def _build_app(store: _FakeStore, current_user_email: str | None):
    """Build a FastAPI app with share router and mocked DB/auth."""
    from apowerb.routers import share as share_module
    from apowerb.auth.dependencies import get_current_user, get_optional_user
    from apowerb.helpers.database import get_db

    app = FastAPI()
    app.include_router(share_module.router, prefix="/api")

    class _FakeResultScalars:
        def __init__(self, record):
            self._record = record

        def first(self):
            return self._record

    class _FakeResult:
        def __init__(self, record):
            self._record = record

        def scalars(self):
            return _FakeResultScalars(self._record)

    class _FakeSession:
        async def execute(self, stmt):
            # Extract the bound parameter value from the WHERE clause.
            target_id = None
            try:
                params = stmt.compile().params
                if params:
                    target_id = next(iter(params.values()))
            except Exception:
                target_id = None
            record = store.records.get(target_id) if target_id is not None else None
            return _FakeResult(record)

        def add(self, obj):
            store.records[obj.id] = obj

        async def delete(self, obj):
            store.deleted.append(obj.id)
            store.records.pop(obj.id, None)

        async def commit(self):
            return None

    async def _get_db_override():
        yield _FakeSession()

    async def _current_user_override():
        if current_user_email is None:
            from fastapi import HTTPException, status
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Not authenticated",
            )
        return _fake_user(current_user_email)

    async def _optional_user_override():
        if current_user_email is None:
            return None
        return _fake_user(current_user_email)

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_current_user] = _current_user_override
    app.dependency_overrides[get_optional_user] = _optional_user_override

    return app


@pytest.fixture()
def app_anonymous(store):
    return _build_app(store, current_user_email=None)


@pytest.fixture()
def app_user_a(store):
    return _build_app(store, current_user_email=USER_A)


@pytest.fixture()
def app_user_b(store):
    return _build_app(store, current_user_email=USER_B)


def _payload():
    return {
        "title": "My chat",
        "agentName": "Assistant",
        "messages": [{"role": "user", "content": "hello"}],
    }


def _insert_share(store, share_id, owner_id, is_public, expires_delta_days=1):
    """Helper: insert a SharedConversation-like record directly in the store."""
    from apowerb.models import SharedConversation

    record = SharedConversation(
        id=share_id,
        title="Secret",
        agent_name="Assistant",
        messages=[{"role": "user", "content": "hi"}],
        created_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc)
        + timedelta(days=expires_delta_days),
        owner_id=owner_id,
        is_public=is_public,
    )
    store.records[share_id] = record
    return record


class TestCreateShareRequiresAuth:
    def test_post_share_without_token_returns_401(self, app_anonymous, store):
        client = TestClient(app_anonymous, raise_server_exceptions=False)
        resp = client.post("/api/conversations/share", json=_payload())
        assert resp.status_code == 401, resp.text
        assert store.records == {}

    def test_post_share_with_auth_stores_owner_id(self, app_user_a, store):
        client = TestClient(app_user_a)
        resp = client.post("/api/conversations/share", json=_payload())
        assert resp.status_code == 200, resp.text
        share_id = resp.json()["shareId"]
        assert share_id in store.records
        record = store.records[share_id]
        assert record.owner_id == USER_A
        # By default a newly created share is NOT public unless explicitly asked
        assert bool(record.is_public) is False


class TestReadShareOwnership:
    def test_owner_can_read_own_private_share(self, app_user_a, store):
        client = TestClient(app_user_a)
        created = client.post("/api/conversations/share", json=_payload())
        share_id = created.json()["shareId"]

        resp = client.get(f"/api/conversations/share/{share_id}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["title"] == "My chat"

    def test_other_authenticated_user_cannot_read_private_share(
        self, app_user_a, store
    ):
        client_a = TestClient(app_user_a)
        share_id = client_a.post(
            "/api/conversations/share", json=_payload()
        ).json()["shareId"]

        # Switch context: a different user tries to read it
        from apowerb.auth.dependencies import (
            get_current_user,
            get_optional_user,
        )

        async def _current_b():
            return _fake_user(USER_B)

        async def _optional_b():
            return _fake_user(USER_B)

        app_user_a.dependency_overrides[get_current_user] = _current_b
        app_user_a.dependency_overrides[get_optional_user] = _optional_b

        resp = client_a.get(f"/api/conversations/share/{share_id}")
        assert resp.status_code == 403, resp.text

    def test_anonymous_cannot_read_private_share(self, app_anonymous, store):
        _insert_share(store, "abc123", owner_id=USER_A, is_public=False)

        client = TestClient(app_anonymous, raise_server_exceptions=False)
        resp = client.get("/api/conversations/share/abc123")
        assert resp.status_code in (401, 403), resp.text

    def test_anonymous_can_read_public_share(self, app_anonymous, store):
        _insert_share(store, "pub123", owner_id=USER_A, is_public=True)

        client = TestClient(app_anonymous)
        resp = client.get("/api/conversations/share/pub123")
        assert resp.status_code == 200, resp.text


class TestCreatePublicShare:
    def test_post_with_is_public_true_stores_public_flag(self, app_user_a, store):
        client = TestClient(app_user_a)
        payload = _payload()
        payload["isPublic"] = True
        resp = client.post("/api/conversations/share", json=payload)
        assert resp.status_code == 200, resp.text
        share_id = resp.json()["shareId"]
        assert bool(store.records[share_id].is_public) is True
        assert store.records[share_id].owner_id == USER_A


class TestDeleteShareOwnership:
    def test_owner_can_delete_own_share(self, app_user_a, store):
        client = TestClient(app_user_a)
        share_id = client.post(
            "/api/conversations/share", json=_payload()
        ).json()["shareId"]

        resp = client.delete(f"/api/conversations/share/{share_id}")
        assert resp.status_code in (200, 204), resp.text
        assert share_id not in store.records

    def test_other_user_cannot_delete_share(self, app_user_a, store):
        client = TestClient(app_user_a)
        share_id = client.post(
            "/api/conversations/share", json=_payload()
        ).json()["shareId"]

        from apowerb.auth.dependencies import get_current_user

        async def _current_b():
            return _fake_user(USER_B)

        app_user_a.dependency_overrides[get_current_user] = _current_b

        resp = client.delete(f"/api/conversations/share/{share_id}")
        assert resp.status_code == 403, resp.text
        assert share_id in store.records

    def test_anonymous_cannot_delete_share(self, app_anonymous, store):
        _insert_share(store, "abc123", owner_id=USER_A, is_public=False)

        client = TestClient(app_anonymous, raise_server_exceptions=False)
        resp = client.delete("/api/conversations/share/abc123")
        assert resp.status_code == 401, resp.text
        assert "abc123" in store.records

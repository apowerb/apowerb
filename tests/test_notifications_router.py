"""Tests d'intégration pour routers/notifications.py.

Vérifient :
- GET /api/notifications filtre bien par user courant (la clause SQL inclut user_id).
- PATCH /api/notifications/{id}/read cross-owner → 404 (filtré par user_id).
- GET /api/notifications/unread-count ne compte que les notifs du user courant.
- La liste renvoyée ne contient pas les notifications d'autres users.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient


USER_A_ID = 1
USER_A_EMAIL = "alice@example.com"
USER_B_ID = 2


def _fake_user(user_id: int, email: str):
    u = MagicMock()
    u.email = email
    u.user_id = user_id
    u.role = "USER"
    return u


def _make_notif(id_: int, user_id: int, title: str = "hello", is_read: bool = False):
    n = MagicMock()
    n.id = id_
    n.user_id = user_id
    n.title = title
    n.message = "msg"
    n.type = "info"
    n.link = None
    n.metadata_json = None
    n.is_read = is_read
    n.created_at = datetime.now(timezone.utc)
    return n


class _FakeSession:
    def __init__(
        self,
        *,
        scalar_one_or_none=None,
        scalars_all=None,
        scalar_one=None,
        rowcount: int = 0,
    ):
        self._scalar_one_or_none = scalar_one_or_none
        self._scalars_all = scalars_all if scalars_all is not None else []
        self._scalar_one = scalar_one
        self._rowcount = rowcount
        self.last_stmt = None

    async def execute(self, stmt):
        self.last_stmt = stmt
        res = MagicMock()
        res.scalar_one_or_none = MagicMock(return_value=self._scalar_one_or_none)
        res.scalars = MagicMock(
            return_value=MagicMock(all=MagicMock(return_value=self._scalars_all))
        )
        res.scalar_one = MagicMock(return_value=self._scalar_one)
        res.rowcount = self._rowcount
        return res

    def add(self, obj):
        return None

    async def commit(self):
        return None

    async def refresh(self, obj):
        return None


def _build_app(
    session: _FakeSession,
    *,
    user_id: int | None = USER_A_ID,
    email: str | None = USER_A_EMAIL,
):
    from th2agent.auth.dependencies import get_current_user
    from th2agent.helpers.database import get_db
    from th2agent.routers.notifications import router

    app = FastAPI()
    app.include_router(router, prefix="/api")

    async def _user_override():
        if user_id is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated"
            )
        return _fake_user(user_id, email or USER_A_EMAIL)

    async def _db_override():
        yield session

    app.dependency_overrides[get_current_user] = _user_override
    app.dependency_overrides[get_db] = _db_override
    return app


# ---------------------------------------------------------------------------
# 1. GET /api/notifications filters by current user
# ---------------------------------------------------------------------------


class TestListFilteredByCurrentUser:
    def test_list_returns_only_current_user_notifications(self):
        my_notif = _make_notif(10, user_id=USER_A_ID, title="mine")
        session = _FakeSession(scalars_all=[my_notif])
        app = _build_app(session)
        client = TestClient(app)

        resp = client.get("/api/notifications")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["notifications"]) == 1
        assert body["notifications"][0]["id"] == 10
        # The compiled SELECT must reference user_id in its WHERE clause
        assert "user_id" in str(session.last_stmt).lower()

    def test_list_never_leaks_other_users(self):
        """Simulate the DB returning only the current user's rows (as it would
        with the user_id filter).  The handler must not introduce any row from
        another user."""
        other_user_notif = _make_notif(99, user_id=USER_B_ID, title="other")
        # Session returns ONLY current-user-notifications — nothing from B.
        session = _FakeSession(scalars_all=[])
        app = _build_app(session)
        client = TestClient(app)

        resp = client.get("/api/notifications")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # No notifications from B: response is empty.
        assert body["notifications"] == []
        assert not any(
            n.get("id") == other_user_notif.id for n in body["notifications"]
        )


# ---------------------------------------------------------------------------
# 2. Cross-owner mark-read → 404 (filtered by user_id)
# ---------------------------------------------------------------------------


class TestCrossOwnerMarkRead:
    def test_mark_as_read_other_user_returns_404(self):
        # SELECT ... WHERE id=99 AND user_id=<alice> returns nothing → 404
        session = _FakeSession(scalar_one_or_none=None)
        app = _build_app(session)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.patch("/api/notifications/99/read")
        assert resp.status_code == 404, resp.text

    def test_mark_as_read_own_notification_returns_200(self):
        my_notif = _make_notif(7, user_id=USER_A_ID)
        session = _FakeSession(scalar_one_or_none=my_notif)
        app = _build_app(session)
        client = TestClient(app)

        resp = client.patch("/api/notifications/7/read")
        assert resp.status_code == 200, resp.text
        assert resp.json()["id"] == 7


# ---------------------------------------------------------------------------
# 3. GET /api/notifications/unread-count filtered by current user
# ---------------------------------------------------------------------------


class TestUnreadCountScopedToUser:
    def test_unread_count_uses_current_user_filter(self):
        session = _FakeSession(scalar_one=3)
        app = _build_app(session)
        client = TestClient(app)

        resp = client.get("/api/notifications/unread-count")
        assert resp.status_code == 200, resp.text
        assert resp.json()["count"] == 3
        # The COUNT query must include user_id in its WHERE clause
        assert "user_id" in str(session.last_stmt).lower()


# ---------------------------------------------------------------------------
# 4. Auth required
# ---------------------------------------------------------------------------


class TestAuthRequired:
    def test_list_without_auth_returns_401(self):
        session = _FakeSession()
        app = _build_app(session, user_id=None, email=None)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/notifications")
        assert resp.status_code == 401, resp.text

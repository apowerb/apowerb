"""Tests d'intégration pour routers/webhooks.py.

Vérifient :
- Authentification requise sur les endpoints protégés (401 sans token).
- Happy path : création d'une subscription Outlook (TestClient + override get_current_user + get_db).
- Cross-owner : delete d'une subscription appartenant à un autre user → 404 (via filter SQL).
- GET /api/webhooks/logs : filtre bien par owner (uniquement les logs du user courant).
- Signature invalide sur /api/webhooks/gmail/notifications → 401 (pas d'Authorization header).
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient


USER_A_ID = 1
USER_A_EMAIL = "alice@example.com"
USER_B_ID = 2
USER_B_EMAIL = "bob@example.com"


def _fake_user(user_id: int, email: str):
    u = MagicMock()
    u.email = email
    u.user_id = user_id
    u.role = "USER"
    return u


class _FakeSession:
    """Minimal AsyncSession stub that returns whatever we inject."""

    def __init__(
        self,
        *,
        scalar_one_or_none=None,
        scalars_all=None,
        scalar_one=None,
        scalar=0,
    ):
        self._scalar_one_or_none = scalar_one_or_none
        self._scalars_all = scalars_all if scalars_all is not None else []
        self._scalar_one = scalar_one
        # /logs also asks for a COUNT so the UI can report how many rows match
        # the filters without walking the pages.
        self._scalar = scalar
        self._added: list = []
        self._deleted: list = []
        self.committed = False

    async def scalar(self, stmt):
        return self._scalar

    async def execute(self, stmt):
        res = MagicMock()
        res.scalar_one_or_none = MagicMock(
            return_value=self._scalar_one_or_none
        )
        res.scalars = MagicMock(
            return_value=MagicMock(all=MagicMock(return_value=self._scalars_all))
        )
        res.scalar_one = MagicMock(return_value=self._scalar_one)
        res.rowcount = 0
        return res

    def add(self, obj):
        self._added.append(obj)

    async def commit(self):
        self.committed = True

    async def refresh(self, obj):
        return None

    async def delete(self, obj):
        self._deleted.append(obj)


def _build_app(session: _FakeSession, *, user_id: int | None = USER_A_ID, email: str | None = USER_A_EMAIL):
    from apowerb.auth.dependencies import get_current_user
    from apowerb.helpers.database import get_db
    from apowerb.routers.webhooks import router

    app = FastAPI()
    app.include_router(router, prefix="/api")

    async def _user_override():
        if user_id is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Not authenticated",
            )
        return _fake_user(user_id, email or USER_A_EMAIL)

    async def _db_override():
        yield session

    app.dependency_overrides[get_current_user] = _user_override
    app.dependency_overrides[get_db] = _db_override
    return app


# ---------------------------------------------------------------------------
# 1. Auth required
# ---------------------------------------------------------------------------


class TestAuthRequired:
    def test_create_subscription_without_auth_returns_401(self):
        app = _build_app(_FakeSession(), user_id=None, email=None)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/api/webhooks/subscriptions",
            json={
                "provider": "microsoft_outlook",
                "resource": "me/mailFolders('Inbox')/messages",
                "change_type": "created",
                "agent_id": 1,
            },
        )
        assert resp.status_code == 401, resp.text

    def test_list_subscriptions_without_auth_returns_401(self):
        app = _build_app(_FakeSession(), user_id=None, email=None)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/webhooks/subscriptions")
        assert resp.status_code == 401, resp.text

    def test_logs_without_auth_returns_401(self):
        app = _build_app(_FakeSession(), user_id=None, email=None)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/webhooks/logs")
        assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# 2. Create subscription (happy path, Outlook)
# ---------------------------------------------------------------------------


class TestCreateSubscriptionHappyPath:
    def test_create_outlook_subscription_owner(self):
        # Fake integration row returned when the router looks it up
        integration = MagicMock()
        integration.id = 42

        saved_sub = MagicMock()
        saved_sub.id = 101
        saved_sub.provider = "microsoft_outlook"
        saved_sub.subscription_id = "graph-sub-xyz"
        saved_sub.resource = "me/mailFolders('Inbox')/messages"
        saved_sub.change_type = "created"
        saved_sub.agent_id = 1
        saved_sub.agent_message_template = None
        saved_sub.status = "active"
        saved_sub.expiration_datetime = datetime.now(timezone.utc)
        saved_sub.created_at = datetime.now(timezone.utc)

        session = _FakeSession(
            scalar_one_or_none=integration,
            scalar_one=saved_sub,
        )
        app = _build_app(session)
        client = TestClient(app)

        with patch(
            "apowerb.routers.webhooks.OutlookWebhookService.get_access_token_for_user",
            new_callable=AsyncMock,
            return_value="fake-token",
        ), patch(
            "apowerb.routers.webhooks.OutlookWebhookService.generate_client_state",
            return_value="cs",
        ), patch(
            "apowerb.routers.webhooks.OutlookWebhookService.create_subscription",
            new_callable=AsyncMock,
            return_value={
                "id": "graph-sub-xyz",
                "expirationDateTime": "2030-01-01T00:00:00Z",
            },
        ):
            resp = client.post(
                "/api/webhooks/subscriptions",
                json={
                    "provider": "microsoft_outlook",
                    "resource": "me/mailFolders('Inbox')/messages",
                    "change_type": "created",
                    "agent_id": 1,
                },
            )

        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["id"] == 101
        assert body["subscription_id"] == "graph-sub-xyz"
        assert session.committed


# ---------------------------------------------------------------------------
# 3. Cross-owner delete  → 403 (distinguishes "not yours" from "not found")
# ---------------------------------------------------------------------------


class _TwoLookupsFakeSession:
    """Fake session that returns different scalar_one_or_none values for
    successive ``execute()`` calls (first lookup = without owner filter,
    second lookup = with owner filter).
    """

    def __init__(self, lookups: list):
        self._lookups = list(lookups)
        self._added: list = []
        self._deleted: list = []
        self.committed = False

    async def execute(self, stmt):
        res = MagicMock()
        val = self._lookups.pop(0) if self._lookups else None
        res.scalar_one_or_none = MagicMock(return_value=val)
        res.scalars = MagicMock(
            return_value=MagicMock(all=MagicMock(return_value=[]))
        )
        res.scalar_one = MagicMock(return_value=val)
        res.rowcount = 0
        return res

    def add(self, obj):
        self._added.append(obj)

    async def commit(self):
        self.committed = True

    async def refresh(self, obj):
        return None

    async def delete(self, obj):
        self._deleted.append(obj)


class TestCrossOwnerDelete:
    def test_delete_nonexistent_subscription_returns_404(self):
        # No row exists at all for id=5 → 404
        session = _TwoLookupsFakeSession(lookups=[None])
        app = _build_app(session)  # alice
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.delete("/api/webhooks/subscriptions/5")
        assert resp.status_code == 404, resp.text

    def test_delete_other_user_subscription_returns_403(self):
        # Row exists but owned by bob (user_id=2); alice (user_id=1) tries to delete → 403
        bob_sub = MagicMock()
        bob_sub.id = 5
        bob_sub.user_id = USER_B_ID
        bob_sub.provider = "microsoft_outlook"
        bob_sub.subscription_id = "graph-sub-bob"
        session = _TwoLookupsFakeSession(lookups=[bob_sub])
        app = _build_app(session)  # alice
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.delete("/api/webhooks/subscriptions/5")
        assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# 4. GET /api/webhooks/logs filtered by owner
# ---------------------------------------------------------------------------


class TestLogsFilteredByOwner:
    def test_logs_returns_only_current_user_logs(self):
        log1 = MagicMock()
        log1.id = 10
        log1.subscription_id = 1
        log1.agent_id = 1
        log1.trigger_event = "created"
        log1.email_subject = "hi"
        log1.email_sender = "x@y"
        log1.agent_message = "msg"
        log1.agent_response = "resp"
        log1.status = "success"
        log1.error_message = None
        log1.duration_ms = 12
        log1.created_at = datetime.now(timezone.utc)

        session = _FakeSession(scalars_all=[log1])
        app = _build_app(session)
        client = TestClient(app)

        # Capture the WHERE clauses passed to session.execute so we can assert
        # the query filters by the current user's id.
        captured: dict = {}
        original_execute = session.execute

        async def _spy_execute(stmt):
            captured["stmt"] = stmt
            return await original_execute(stmt)

        session.execute = _spy_execute  # type: ignore[assignment]

        resp = client.get("/api/webhooks/logs")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["logs"]) == 1
        assert body["logs"][0]["id"] == 10
        # Ensure the statement was built with a user_id equality clause —
        # the compile output should contain "user_id" as a bind param.
        assert "user_id" in str(captured["stmt"]).lower()


# ---------------------------------------------------------------------------
# 5. Incoming HMAC/OIDC signature invalid → 401
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 3c. Delete happy-path: no ORM access after commit (MissingGreenlet regression)
# ---------------------------------------------------------------------------


class _ExpiringSubscription:
    """ORM-like stub whose ``provider``/``subscription_id`` raise once the
    session has committed -- mirrors SQLAlchemy expiring the instance after
    ``commit()``, which under asyncpg triggers ``MissingGreenlet`` if accessed
    outside a greenlet (the prod bug at webhooks.py:419/426).
    """

    def __init__(self, **attrs):
        object.__setattr__(self, "_attrs", attrs)
        object.__setattr__(self, "_expired", False)

    def _expire(self):
        object.__setattr__(self, "_expired", True)

    def __getattr__(self, name):
        if object.__getattribute__(self, "_expired") and name in (
            "provider",
            "subscription_id",
        ):
            raise RuntimeError(
                "MissingGreenlet: ORM attribute accessed after commit"
            )
        try:
            return object.__getattribute__(self, "_attrs")[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


class _ExpiringFakeSession:
    def __init__(self, subscription):
        self._subscription = subscription
        self.committed = False
        self._deleted: list = []

    async def execute(self, stmt):
        res = MagicMock()
        res.scalar_one_or_none = MagicMock(return_value=self._subscription)
        return res

    async def delete(self, obj):
        self._deleted.append(obj)

    async def commit(self):
        self.committed = True
        # SQLAlchemy expires instances on commit by default.
        self._subscription._expire()


class TestDeleteSubscriptionPostCommitAccess:
    def test_delete_outlook_does_not_read_orm_after_commit(self):
        sub = _ExpiringSubscription(
            id=5,
            user_id=USER_A_ID,
            provider="microsoft_outlook",
            subscription_id="graph-sub-xyz",
        )
        session = _ExpiringFakeSession(sub)
        app = _build_app(session)  # alice owns it
        client = TestClient(app, raise_server_exceptions=False)

        with patch(
            "apowerb.routers.webhooks.OutlookWebhookService.get_access_token_for_user",
            new_callable=AsyncMock,
            return_value="fake-token",
        ), patch(
            "apowerb.routers.webhooks.OutlookWebhookService.delete_subscription",
            new_callable=AsyncMock,
            return_value=None,
        ):
            resp = client.delete("/api/webhooks/subscriptions/5")

        assert resp.status_code == 200, resp.text
        assert session.committed
        assert session._deleted == [sub]


class TestGmailIncomingSignatureInvalid:
    def test_gmail_notification_missing_auth_returns_401(self):
        """The public /webhooks/gmail/notifications endpoint requires an OIDC
        Bearer token (Google Pub/Sub).  Missing Authorization header → 401."""
        from apowerb.configs.settings import Settings, get_settings
        from apowerb.routers.webhooks import router

        def _override_settings() -> Settings:
            return Settings(
                db_host="x",
                db_name="x",
                db_user="x",
                db_password="x",
                test_token="x",
                encrypt_key="x",
                google_webhook_audience="https://webhook.th2ai.test/gmail",
                webhook_dev_skip_sig=False,
            )

        app = FastAPI()
        app.include_router(router, prefix="/api")
        app.dependency_overrides[get_settings] = _override_settings

        with patch(
            "apowerb.routers.webhook_handlers.gmail.get_settings",
            side_effect=_override_settings,
        ):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post(
                "/api/webhooks/gmail/notifications",
                json={
                    "message": {"data": "eyJlbWFpbCI6ICJhQGIuY29tIn0="},
                    "subscription": "projects/x/subscriptions/y",
                },
            )
        assert resp.status_code == 401, resp.text



# ---------------------------------------------------------------------------
# 6. GET /api/webhooks/logs/{log_id}
# ---------------------------------------------------------------------------


class TestGetLogById:
    """Single-log fetch supports the dashboard deep-link
    ``/webhooks?log=<id>`` so the Activity tab can expand a row that is
    not in the currently loaded page."""

    def _make_log(self, log_id: int):
        log = MagicMock()
        log.id = log_id
        log.user_id = USER_A_ID
        log.attachments = []
        log.subscription_id = 1
        log.agent_id = 1
        log.trigger_event = "created"
        log.email_subject = "deep-linked email"
        log.email_sender = "a@b"
        log.agent_message = "msg"
        log.agent_response = "resp"
        log.status = "success"
        log.error_message = None
        log.duration_ms = 42
        log.created_at = datetime.now(timezone.utc)
        return log

    def test_returns_log_when_owned_by_current_user(self):
        log = self._make_log(2362)
        session = _FakeSession(scalar_one_or_none=log)
        app = _build_app(session)
        client = TestClient(app)

        captured: dict = {}
        original_execute = session.execute

        async def _spy_execute(stmt):
            captured["stmt"] = stmt
            return await original_execute(stmt)

        session.execute = _spy_execute  # type: ignore[assignment]

        resp = client.get("/api/webhooks/logs/2362")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["log"]["id"] == 2362
        assert body["log"]["email_subject"] == "deep-linked email"
        # Defence-in-depth: query must scope by user_id.
        assert "user_id" in str(captured["stmt"]).lower()

    def test_returns_404_when_log_missing(self):
        session = _FakeSession(scalar_one_or_none=None)
        app = _build_app(session)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/api/webhooks/logs/99999")
        assert resp.status_code == 404, resp.text

    def test_requires_authentication(self):
        session = _FakeSession(scalar_one_or_none=None)
        app = _build_app(session, user_id=None, email=None)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/api/webhooks/logs/2362")
        assert resp.status_code == 401, resp.text


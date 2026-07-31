"""Tests for B18 — forgot-password + reset-password endpoints.

Covers:
  - POST /api/auth/forgot-password with unknown email   -> 200 (no leak)
  - POST /api/auth/forgot-password with known email     -> 200 + email dispatched
  - POST /api/auth/reset-password with valid token      -> 200, password updated
  - POST /api/auth/reset-password with invalid token    -> 401
  - POST /api/auth/reset-password with expired token    -> 401
  - POST /api/auth/reset-password with wrong token type -> 401
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jose import jwt


USER_EMAIL = "alice@example.com"
USER_OLD_PASSWORD = "OldP@ssword1"
USER_NEW_PASSWORD = "NewP@ssword2"


class _FakeUser:
    """Minimal attribute-based stand-in for the ORM ``User`` row."""

    def __init__(self, email: str, hashed: str, user_id: int = 1):
        self.email = email
        self.password = hashed
        self.user_id = user_id
        mocked_role = MagicMock()
        mocked_role.name = "USER"
        mocked_role.value = "USER"
        self.role = mocked_role
        self.mfa_enabled = False


class _FakeResult:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


class _FakeSession:
    """In-memory DB stand-in: a single user keyed by email."""

    def __init__(self, user=None):
        self._user = user
        self.committed = False

    async def execute(self, stmt):
        return _FakeResult(self._user)

    async def commit(self):
        self.committed = True

    async def refresh(self, obj):
        return None


@pytest.fixture()
def sent_emails():
    return []


@pytest.fixture()
def app_with_auth(monkeypatch, sent_emails):
    """Mount the auth router with a fake DB and a fake email sender."""
    from apowerb.auth import router as auth_router_module
    from apowerb.auth.dependencies import get_db
    from apowerb.helpers.security import get_password_hash

    hashed = get_password_hash(USER_OLD_PASSWORD)
    fake_user = _FakeUser(USER_EMAIL, hashed)
    session = _FakeSession(fake_user)

    async def _db_override():
        yield session

    app = FastAPI()
    app.include_router(auth_router_module.router, prefix="/api")
    app.dependency_overrides[get_db] = _db_override

    # Patch the email sender to capture outbound messages. The production
    # helper lives in ``helpers.email_sender`` and the auth service imports
    # it lazily — so we patch at the source module.
    from apowerb.helpers import system_mailer

    async def _fake_send(*, to, subject, html, text=None):
        sent_emails.append({"to": to, "subject": subject, "body": html})
        return True

    monkeypatch.setattr(system_mailer, "send_system_email", _fake_send)

    return app, session, fake_user


class TestForgotPassword:
    def test_forgot_password_unknown_email_returns_200(
        self, monkeypatch, sent_emails
    ):
        """Unknown emails must NOT leak their existence."""
        from apowerb.auth import router as auth_router_module
        from apowerb.auth.dependencies import get_db
        from apowerb.helpers import system_mailer

        async def _db_override():
            yield _FakeSession(None)  # no user

        app = FastAPI()
        app.include_router(auth_router_module.router, prefix="/api")
        app.dependency_overrides[get_db] = _db_override

        async def _fake_send(*, to, subject, html, text=None):
            sent_emails.append({"to": to})
            return True

        monkeypatch.setattr(system_mailer, "send_system_email", _fake_send)

        client = TestClient(app)
        resp = client.post(
            "/api/auth/forgot-password",
            json={"email": "unknown@example.com"},
        )
        assert resp.status_code == 200, resp.text
        # And no email must have been dispatched
        assert sent_emails == []

    def test_forgot_password_known_email_sends_reset(
        self, app_with_auth, sent_emails
    ):
        app, _, _ = app_with_auth
        client = TestClient(app)

        resp = client.post(
            "/api/auth/forgot-password",
            json={"email": USER_EMAIL},
        )
        assert resp.status_code == 200, resp.text
        assert len(sent_emails) == 1
        assert sent_emails[0]["to"] == USER_EMAIL


class TestResetPassword:
    def _mint(self, *, email: str, token_type: str = "password_reset",
              expires_delta: timedelta = timedelta(minutes=30)):
        from apowerb.helpers.security import get_secret_key, get_algorithm

        payload = {
            "sub": email,
            "type": token_type,
            "exp": datetime.now(timezone.utc) + expires_delta,
        }
        return jwt.encode(payload, get_secret_key(), algorithm=get_algorithm())

    def test_reset_password_valid_token_updates_password(
        self, app_with_auth
    ):
        app, session, user = app_with_auth
        client = TestClient(app)

        token = self._mint(email=USER_EMAIL)
        resp = client.post(
            "/api/auth/reset-password",
            json={"token": token, "new_password": USER_NEW_PASSWORD},
        )
        assert resp.status_code == 200, resp.text

        # Password hash must have been rotated AND persisted
        from apowerb.helpers.security import verify_password
        assert verify_password(USER_NEW_PASSWORD, user.password)
        assert session.committed

    def test_reset_password_invalid_token_returns_401(self, app_with_auth):
        app, _, _ = app_with_auth
        client = TestClient(app)

        resp = client.post(
            "/api/auth/reset-password",
            json={"token": "not-a-jwt", "new_password": USER_NEW_PASSWORD},
        )
        assert resp.status_code == 401, resp.text

    def test_reset_password_expired_token_returns_401(self, app_with_auth):
        app, _, _ = app_with_auth
        client = TestClient(app)

        token = self._mint(
            email=USER_EMAIL,
            expires_delta=timedelta(minutes=-1),  # already expired
        )
        resp = client.post(
            "/api/auth/reset-password",
            json={"token": token, "new_password": USER_NEW_PASSWORD},
        )
        assert resp.status_code == 401, resp.text

    def test_reset_password_wrong_token_type_returns_401(self, app_with_auth):
        app, _, _ = app_with_auth
        client = TestClient(app)

        # "access" is a valid JWT type, but NOT for password reset.
        token = self._mint(email=USER_EMAIL, token_type="access")
        resp = client.post(
            "/api/auth/reset-password",
            json={"token": token, "new_password": USER_NEW_PASSWORD},
        )
        assert resp.status_code == 401, resp.text


class TestResetTokenIssuance:
    """Direct unit test of ``generate_reset_token`` — it must mint a JWT
    scoped to ``type=password_reset`` with the user's email in ``sub`` and a
    30-minute TTL by default."""

    def test_generate_reset_token_encodes_type_and_sub(self):
        from apowerb.auth.service import generate_reset_token
        from apowerb.helpers.security import get_secret_key, get_algorithm

        token = generate_reset_token(USER_EMAIL)
        payload = jwt.decode(token, get_secret_key(), algorithms=[get_algorithm()])
        assert payload["sub"] == USER_EMAIL
        assert payload["type"] == "password_reset"
        assert "exp" in payload

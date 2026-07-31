"""HTTP-level flow test for email verification — proves the exact contract the
front-end keys on: login 403 detail=email_not_verified, verify-email 200,
resend 200. Mounts the real auth router with a stateful fake DB (no server, no
network, no real mail)."""

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apowerb.auth import router as auth_router_module
from apowerb.auth import service
from apowerb.auth.dependencies import get_db
from apowerb.helpers.security import get_password_hash

EMAIL = "flow@example.com"
PASSWORD = "GoodP@ss1"


class _FakeUser:
    def __init__(self):
        self.email = EMAIL
        self.password = get_password_hash(PASSWORD)
        self.user_id = 1
        self.email_verified = False
        self.mfa_enabled = False
        role = MagicMock()
        role.name = "USER"
        role.value = "USER"
        self.role = role


class _FakeResult:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


class _FakeSession:
    """Stateful single-user stand-in shared across requests in one test."""

    def __init__(self, user):
        self._user = user

    async def execute(self, stmt):
        return _FakeResult(self._user)

    async def commit(self):
        return None

    async def refresh(self, obj):
        return None


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(service.settings, "auth_email_verification_enabled", True)
    user = _FakeUser()
    session = _FakeSession(user)

    app = FastAPI()
    app.include_router(auth_router_module.router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: session
    return TestClient(app), user


def _login(client):
    return client.post(
        "/api/auth/token",
        data={"username": EMAIL, "password": PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )


def test_full_flow_unverified_then_verified(client):
    c, user = client

    # 1. Unverified login is rejected with the exact contract the front matches.
    r = _login(c)
    assert r.status_code == 403
    assert r.json()["detail"] == "email_not_verified"

    # 2. Consume a verification token.
    token = service.generate_verify_token(EMAIL)
    r = c.post("/api/auth/verify-email", json={"token": token})
    assert r.status_code == 200
    assert user.email_verified is True

    # 3. Login now succeeds and returns a bearer token.
    r = _login(c)
    assert r.status_code == 200
    assert r.json()["access_token"]


def test_verify_email_invalid_token_401(client):
    c, _ = client
    r = c.post("/api/auth/verify-email", json={"token": "garbage.token.here"})
    assert r.status_code == 401


def test_resend_verification_always_200(client, monkeypatch):
    c, _ = client
    # Mailer is mocked: the fake session returns a user regardless of email, so
    # the endpoint would otherwise attempt a real Graph send.
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        "apowerb.helpers.system_mailer.send_system_email", AsyncMock()
    )
    r = c.post("/api/auth/resend-verification", json={"email": "stranger@example.com"})
    assert r.status_code == 200

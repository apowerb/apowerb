"""Tests pour H1 — rejet des tokens `type=agent_refresh` comme access token.

Vérifient que :
- Un JWT `type=access` valide traverse `get_current_user` et le middleware ADK.
- Un JWT `type=agent_refresh` (90 jours) est refusé avec 401 partout où un
  access token est attendu.
- Un JWT sans claim `type` est refusé (stance stricte : rejet plutôt que
  permissif).
- Un JWT `type=refresh` (cookie de session) est refusé en access.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from jose import jwt

from th2agent.auth.dependencies import get_current_user
from th2agent.configs.settings import get_settings
from th2agent.helpers.database import get_db
from th2agent.main import ADKAuthMiddleware


USER_EMAIL = "alice@example.com"
settings = get_settings()
SIGNING_KEY = settings.encrypt_key
ALGO = settings.algorithm


def _make_token(claims: dict) -> str:
    base = {
        "sub": USER_EMAIL,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=30),
    }
    base.update(claims)
    return jwt.encode(base, SIGNING_KEY, algorithm=ALGO)


def _fake_user_row():
    u = MagicMock()
    u.user_id = 1
    u.email = USER_EMAIL
    u.role = MagicMock()
    u.role.value = "USER"
    u.first_name = "Alice"
    u.last_name = "Tester"
    u.username = None
    u.full_name = None
    u.avatar_url = None
    u.plan = "free"
    u.stripe_customer_id = None
    u.mfa_enabled = False
    u.created_at = datetime.now(timezone.utc)
    u.updated_at = datetime.now(timezone.utc)
    return u


class _FakeScalarResult:
    def __init__(self, user):
        self._user = user

    def scalar_one_or_none(self):
        return self._user


class _FakeSession:
    def __init__(self, user):
        self._user = user

    async def execute(self, stmt):
        return _FakeScalarResult(self._user)


@pytest.fixture()
def dep_app():
    """FastAPI app with a single endpoint protected by get_current_user."""
    app = FastAPI()
    user_row = _fake_user_row()

    async def _db_override():
        yield _FakeSession(user_row)

    app.dependency_overrides[get_db] = _db_override

    @app.get("/whoami")
    async def whoami(user=Depends(get_current_user)):
        return {"email": user.email}

    return app


class TestGetCurrentUserTokenType:
    def test_access_token_is_accepted(self, dep_app):
        token = _make_token({"type": "access"})
        client = TestClient(dep_app, raise_server_exceptions=False)
        resp = client.get(
            "/whoami", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["email"] == USER_EMAIL

    def test_agent_refresh_token_is_rejected(self, dep_app):
        """A 90-day agent refresh token must NOT be accepted as access."""
        token = _make_token({"type": "agent_refresh", "user_id": USER_EMAIL})
        client = TestClient(dep_app, raise_server_exceptions=False)
        resp = client.get(
            "/whoami", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 401, resp.text

    def test_refresh_token_is_rejected(self, dep_app):
        token = _make_token({"type": "refresh"})
        client = TestClient(dep_app, raise_server_exceptions=False)
        resp = client.get(
            "/whoami", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 401, resp.text

    def test_token_without_type_claim_is_rejected(self, dep_app):
        """Strict stance: a JWT without explicit `type` claim is rejected."""
        token = _make_token({})  # no type
        client = TestClient(dep_app, raise_server_exceptions=False)
        resp = client.get(
            "/whoami", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 401, resp.text

    def test_download_token_is_rejected(self, dep_app):
        token = _make_token({"type": "download"})
        client = TestClient(dep_app, raise_server_exceptions=False)
        resp = client.get(
            "/whoami", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 401, resp.text


class TestAdkMiddlewareTokenType:
    """The ADK-native middleware must reject agent_refresh tokens on /run*."""

    @pytest.fixture()
    def mw_app(self):
        app = FastAPI()
        app.add_middleware(ADKAuthMiddleware)

        @app.get("/run")
        async def run():
            return {"ok": True}

        @app.post("/run_sse")
        async def run_sse():
            return {"ok": True}

        return app

    def test_access_token_passes_middleware(self, mw_app):
        # Middleware signs with settings.encrypt_key (see main.py)
        payload = {
            "sub": USER_EMAIL,
            "type": "access",
            "exp": datetime.now(timezone.utc) + timedelta(minutes=30),
        }
        tok = jwt.encode(
            payload, settings.encrypt_key, algorithm=settings.algorithm
        )
        client = TestClient(mw_app)
        resp = client.get("/run", headers={"Authorization": f"Bearer {tok}"})
        assert resp.status_code == 200, resp.text

    def test_agent_refresh_token_is_rejected(self, mw_app):
        payload = {
            "user_id": USER_EMAIL,
            "type": "agent_refresh",
            "exp": datetime.now(timezone.utc) + timedelta(days=90),
        }
        tok = jwt.encode(
            payload, settings.encrypt_key, algorithm=settings.algorithm
        )
        client = TestClient(mw_app)
        resp = client.get("/run", headers={"Authorization": f"Bearer {tok}"})
        assert resp.status_code == 401, resp.text

    def test_token_without_type_claim_is_rejected(self, mw_app):
        payload = {
            "sub": USER_EMAIL,
            "exp": datetime.now(timezone.utc) + timedelta(minutes=30),
        }
        tok = jwt.encode(
            payload, settings.encrypt_key, algorithm=settings.algorithm
        )
        client = TestClient(mw_app)
        resp = client.get("/run", headers={"Authorization": f"Bearer {tok}"})
        assert resp.status_code == 401, resp.text

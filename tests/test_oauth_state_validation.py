"""Tests pour C4 — validation CSRF du state OAuth.

Vérifient, pour les 3 providers OAuth (GitHub, Microsoft, Google) :
- Le `state` généré côté connect est bien persisté en DB via le store.
- Le callback sans `state` renvoie 400.
- Le callback avec un `state` inconnu renvoie 400.
- Le callback avec un `state` expiré (>10 min) renvoie 400.
- Le callback avec un `state` dont le user ne correspond pas renvoie 403.
- Le callback happy path consomme le state et renvoie 200.
"""

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


USER_A_EMAIL = "alice@example.com"
USER_B_EMAIL = "bob@example.com"


def _fake_user(email: str, user_id: int = 1):
    u = MagicMock()
    u.email = email
    u.user_id = user_id
    u.role = "USER"
    return u


# -----------------------------------------------------------------------------
# Minimal DB fake for the OAuth state store
# -----------------------------------------------------------------------------
class _OAuthStateStoreStub:
    """In-memory implementation of the helpers.oauth_state_store interface.

    The production store uses SQLAlchemy; tests swap it out for this stub so
    they don't depend on a real database. ``db`` is accepted and ignored so
    the signature matches the production module exactly.
    """

    def __init__(self):
        self._states: dict[str, dict] = {}

    async def create(
        self,
        *,
        db: Any = None,
        user_id: int,
        provider: str,
        ttl_seconds: int = 600,
    ) -> str:
        import secrets as _s
        token = _s.token_urlsafe(32)
        self._states[token] = {
            "user_id": user_id,
            "provider": provider,
            "expires_at": datetime.now(timezone.utc)
            + timedelta(seconds=ttl_seconds),
        }
        return token

    async def consume(
        self,
        *,
        db: Any = None,
        state: str,
        user_id: int,
        provider: str,
    ) -> None:
        from fastapi import HTTPException, status as st

        if not state:
            raise HTTPException(
                status_code=st.HTTP_400_BAD_REQUEST,
                detail="Missing OAuth state parameter.",
            )
        stored = self._states.pop(state, None)
        if not stored:
            raise HTTPException(
                status_code=st.HTTP_400_BAD_REQUEST,
                detail="Invalid or unknown OAuth state.",
            )
        if datetime.now(timezone.utc) >= stored["expires_at"]:
            raise HTTPException(
                status_code=st.HTTP_400_BAD_REQUEST,
                detail="OAuth state expired.",
            )
        if stored["user_id"] != user_id or stored["provider"] != provider:
            raise HTTPException(
                status_code=st.HTTP_403_FORBIDDEN,
                detail="OAuth state does not match the authenticated user.",
            )

    def seed(
        self,
        *,
        state: str,
        user_id: int,
        provider: str,
        expires_at: datetime,
    ) -> None:
        self._states[state] = {
            "user_id": user_id,
            "provider": provider,
            "expires_at": expires_at,
        }

    def has(self, state: str) -> bool:
        return state in self._states


@pytest.fixture()
def store():
    return _OAuthStateStoreStub()


@pytest.fixture()
def app_with_overrides(store):
    """Build a FastAPI app mounting the integrations router with stubs."""
    from th2agent.routers import integrations as integrations_module
    from th2agent.auth.dependencies import get_current_user
    from th2agent.helpers.database import get_db

    # Replace the singleton oauth state store with our in-memory stub.
    # The router module is expected to expose the store via a module-level
    # attribute so tests can swap it out.
    integrations_module.oauth_state_store = store  # type: ignore[attr-defined]

    app = FastAPI()
    app.include_router(integrations_module.router, prefix="/api")

    class _FakeSession:
        async def execute(self, stmt):
            res = MagicMock()
            res.scalar_one_or_none = MagicMock(return_value=None)
            res.scalars = MagicMock(
                return_value=MagicMock(all=MagicMock(return_value=[]))
            )
            return res

        def add(self, obj):
            return None

        async def commit(self):
            return None

        async def refresh(self, obj):
            return None

        async def delete(self, obj):
            return None

    async def _db_override():
        yield _FakeSession()

    async def _user_override():
        return _fake_user(USER_A_EMAIL, user_id=1)

    app.dependency_overrides[get_db] = _db_override
    app.dependency_overrides[get_current_user] = _user_override

    return app


# =============================================================================
# Generic test helpers
# =============================================================================

GITHUB = "github"
MICROSOFT_OUTLOOK = "microsoft_outlook"
GOOGLE_DRIVE = "google_drive"


def _seed_state(store, *, provider: str, user_id: int, ttl_seconds: int = 600) -> str:
    import secrets as _s
    tok = _s.token_urlsafe(32)
    store.seed(
        state=tok,
        user_id=user_id,
        provider=provider,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds),
    )
    return tok


def _seed_expired_state(store, *, provider: str, user_id: int) -> str:
    import secrets as _s
    tok = _s.token_urlsafe(32)
    store.seed(
        state=tok,
        user_id=user_id,
        provider=provider,
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=10),
    )
    return tok


# =============================================================================
# GitHub
# =============================================================================

class TestGitHubStateValidation:
    def test_connect_persists_state_in_store(self, app_with_overrides, store):
        client = TestClient(app_with_overrides)
        resp = client.get("/api/integrations/github/connect")
        assert resp.status_code == 200, resp.text
        state = resp.json()["state"]
        assert store.has(state), "state should be persisted server-side"

    def test_callback_without_state_returns_400(self, app_with_overrides, store):
        client = TestClient(app_with_overrides)
        resp = client.post(
            "/api/integrations/github/callback",
            json={"code": "xyz", "state": ""},
        )
        # Either 400 (empty state) or 422 (pydantic rejects empty) is acceptable.
        assert resp.status_code in (400, 422), resp.text

    def test_callback_with_unknown_state_returns_400(
        self, app_with_overrides, store
    ):
        client = TestClient(app_with_overrides)
        resp = client.post(
            "/api/integrations/github/callback",
            json={"code": "xyz", "state": "not-in-store"},
        )
        assert resp.status_code == 400, resp.text

    def test_callback_with_expired_state_returns_400(
        self, app_with_overrides, store
    ):
        expired = _seed_expired_state(store, provider=GITHUB, user_id=1)
        client = TestClient(app_with_overrides)
        resp = client.post(
            "/api/integrations/github/callback",
            json={"code": "xyz", "state": expired},
        )
        assert resp.status_code == 400, resp.text

    def test_callback_state_user_mismatch_returns_403(
        self, app_with_overrides, store
    ):
        """State created by user 2 but used by user 1 (authenticated) → 403."""
        mismatched = _seed_state(store, provider=GITHUB, user_id=999)
        client = TestClient(app_with_overrides)
        resp = client.post(
            "/api/integrations/github/callback",
            json={"code": "xyz", "state": mismatched},
        )
        assert resp.status_code == 403, resp.text

    def test_callback_happy_path_consumes_state(
        self, app_with_overrides, store
    ):
        ok_state = _seed_state(store, provider=GITHUB, user_id=1)

        with patch(
            "th2agent.routers.integrations.GitHubIntegrationService."
            "exchange_code_for_token",
            new_callable=AsyncMock,
        ) as exch, patch(
            "th2agent.routers.integrations.GitHubIntegrationService."
            "get_github_user",
            new_callable=AsyncMock,
        ) as gu, patch(
            "th2agent.routers.integrations.GitHubIntegrationService."
            "save_integration",
            new_callable=AsyncMock,
        ) as save:
            exch.return_value = {"access_token": "ghs_xxx", "scope": "repo"}
            gu.return_value = {"id": 1, "login": "alice"}
            integration = MagicMock()
            integration.provider_username = "alice"
            integration.scopes = "repo"
            integration.meta = None
            save.return_value = integration

            client = TestClient(app_with_overrides)
            resp = client.post(
                "/api/integrations/github/callback",
                json={"code": "xyz", "state": ok_state},
            )
        assert resp.status_code == 200, resp.text
        assert not store.has(ok_state), "state should be consumed (one-time)"


# =============================================================================
# Microsoft (per-service callback)
# =============================================================================

class TestMicrosoftStateValidation:
    def test_connect_persists_state(self, app_with_overrides, store):
        client = TestClient(app_with_overrides)
        resp = client.get("/api/integrations/microsoft/outlook/connect")
        assert resp.status_code == 200, resp.text
        state = resp.json()["state"]
        assert store.has(state)

    def test_callback_with_unknown_state_returns_400(
        self, app_with_overrides, store
    ):
        client = TestClient(app_with_overrides)
        resp = client.post(
            "/api/integrations/microsoft/outlook/callback",
            json={
                "code": "c",
                "state": "not-in-store",
                "service": "outlook",
            },
        )
        assert resp.status_code == 400, resp.text

    def test_callback_with_expired_state_returns_400(
        self, app_with_overrides, store
    ):
        expired = _seed_expired_state(
            store, provider=MICROSOFT_OUTLOOK, user_id=1
        )
        client = TestClient(app_with_overrides)
        resp = client.post(
            "/api/integrations/microsoft/outlook/callback",
            json={"code": "c", "state": expired, "service": "outlook"},
        )
        assert resp.status_code == 400, resp.text

    def test_callback_state_user_mismatch_returns_403(
        self, app_with_overrides, store
    ):
        mismatched = _seed_state(
            store, provider=MICROSOFT_OUTLOOK, user_id=999
        )
        client = TestClient(app_with_overrides)
        resp = client.post(
            "/api/integrations/microsoft/outlook/callback",
            json={"code": "c", "state": mismatched, "service": "outlook"},
        )
        assert resp.status_code == 403, resp.text

    def test_callback_happy_path(self, app_with_overrides, store):
        ok_state = _seed_state(store, provider=MICROSOFT_OUTLOOK, user_id=1)

        with patch(
            "th2agent.routers.integrations.MicrosoftIntegrationService."
            "exchange_code_for_token",
            new_callable=AsyncMock,
        ) as exch, patch(
            "th2agent.routers.integrations.MicrosoftIntegrationService."
            "get_microsoft_user",
            new_callable=AsyncMock,
        ) as mu, patch(
            "th2agent.routers.integrations.MicrosoftIntegrationService."
            "save_integration",
            new_callable=AsyncMock,
        ) as save, patch(
            "th2agent.routers.integrations._reset_outlook_module_state"
        ):
            exch.return_value = {
                "access_token": "ms_xxx",
                "refresh_token": "ms_rt",
                "scope": "Mail.Read",
            }
            mu.return_value = {"id": 1, "mail": "alice@example.com"}
            integration = MagicMock()
            integration.provider_username = "alice@example.com"
            integration.scopes = "Mail.Read"
            integration.meta = None
            save.return_value = integration

            client = TestClient(app_with_overrides)
            resp = client.post(
                "/api/integrations/microsoft/outlook/callback",
                json={
                    "code": "c",
                    "state": ok_state,
                    "service": "outlook",
                },
            )
        assert resp.status_code == 200, resp.text
        assert not store.has(ok_state)


# =============================================================================
# Google
# =============================================================================

class TestGoogleStateValidation:
    def test_connect_persists_state(self, app_with_overrides, store):
        client = TestClient(app_with_overrides)
        resp = client.get(
            "/api/integrations/google/connect?service=google_drive"
        )
        assert resp.status_code == 200, resp.text
        state = resp.json()["state"]
        assert store.has(state)

    def test_callback_with_unknown_state_returns_400(
        self, app_with_overrides, store
    ):
        client = TestClient(app_with_overrides)
        resp = client.post(
            "/api/integrations/google/callback",
            json={
                "code": "c",
                "state": "not-in-store",
                "service": "google_drive",
            },
        )
        assert resp.status_code == 400, resp.text

    def test_callback_with_expired_state_returns_400(
        self, app_with_overrides, store
    ):
        expired = _seed_expired_state(
            store, provider=GOOGLE_DRIVE, user_id=1
        )
        client = TestClient(app_with_overrides)
        resp = client.post(
            "/api/integrations/google/callback",
            json={
                "code": "c",
                "state": expired,
                "service": "google_drive",
            },
        )
        assert resp.status_code == 400, resp.text

    def test_callback_state_user_mismatch_returns_403(
        self, app_with_overrides, store
    ):
        mismatched = _seed_state(store, provider=GOOGLE_DRIVE, user_id=999)
        client = TestClient(app_with_overrides)
        resp = client.post(
            "/api/integrations/google/callback",
            json={
                "code": "c",
                "state": mismatched,
                "service": "google_drive",
            },
        )
        assert resp.status_code == 403, resp.text

    def test_callback_happy_path(self, app_with_overrides, store):
        ok_state = _seed_state(store, provider=GOOGLE_DRIVE, user_id=1)

        with patch(
            "th2agent.routers.integrations.GoogleIntegrationService."
            "exchange_code_for_token",
            new_callable=AsyncMock,
        ) as exch, patch(
            "th2agent.routers.integrations.GoogleIntegrationService."
            "get_google_user",
            new_callable=AsyncMock,
        ) as gu, patch(
            "th2agent.routers.integrations.GoogleIntegrationService."
            "save_integration",
            new_callable=AsyncMock,
        ) as save, patch(
            "th2agent.routers.integrations._reset_google_drive_module_state"
        ):
            exch.return_value = {
                "access_token": "g_xxx",
                "refresh_token": "g_rt",
                "scope": "drive.readonly",
            }
            gu.return_value = {"id": 1, "email": "alice@example.com"}
            integration = MagicMock()
            integration.provider_username = "alice@example.com"
            integration.scopes = "drive.readonly"
            integration.meta = None
            save.return_value = integration

            client = TestClient(app_with_overrides)
            resp = client.post(
                "/api/integrations/google/callback",
                json={
                    "code": "c",
                    "state": ok_state,
                    "service": "google_drive",
                },
            )
        assert resp.status_code == 200, resp.text
        assert not store.has(ok_state)

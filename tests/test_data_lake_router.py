"""Tests d'intégration pour routers/data_lake.py.

Vérifient :
- Tous les endpoints exigent l'authentification (401 sans token).
- Liste/read/write délèguent au StorageBoardFactory (mocked) sans mélanger les scopes.
- Les erreurs du board se transforment en 500 côté API.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient


USER_A_EMAIL = "alice@example.com"
USER_B_EMAIL = "bob@example.com"


def _fake_user(email: str, user_id: int = 1):
    u = MagicMock()
    u.email = email
    u.user_id = user_id
    u.role = "USER"
    return u


def _build_app(*, user_email: str | None = USER_A_EMAIL):
    from apowerb.auth.dependencies import get_current_user
    from apowerb.routers.data_lake import router

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
# 1. All endpoints require auth
# ---------------------------------------------------------------------------


class TestAuthRequired:
    def test_pins_list_without_auth_returns_401(self):
        app = _build_app(user_email=None)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/api/data-lake/pins/list",
            json={
                "bucket_name": "bucket",
                "prefix": "p/",
                "storage_source": "s3",
            },
        )
        assert resp.status_code == 401, resp.text

    def test_pins_read_without_auth_returns_401(self):
        app = _build_app(user_email=None)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/api/data-lake/pins/read?pin_name=foo",
            json={
                "bucket_name": "bucket",
                "prefix": "p/",
                "storage_source": "s3",
            },
        )
        assert resp.status_code == 401, resp.text

    def test_pins_write_without_auth_returns_401(self):
        app = _build_app(user_email=None)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/api/data-lake/pins/write",
            json={
                "bucket_name": "bucket",
                "prefix": "p/",
                "storage_source": "s3",
                "pin_name": "foo",
                "data": [{"a": 1}],
                "pin_type": "parquet",
            },
        )
        assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# 2. Pin list — delegates to the board configured with the given bucket/prefix
# ---------------------------------------------------------------------------


class TestPinListScopedByBoardConfig:
    def test_list_pins_builds_board_from_body(self):
        """Ownership isolation in the data lake is enforced via the bucket
        name + prefix supplied in the body.  This test asserts that those
        values are forwarded verbatim to the factory — a user can't read
        pins from a bucket/prefix they don't pass."""

        fake_board = MagicMock()
        fake_list = MagicMock()
        fake_list.to_dict = MagicMock(return_value=[{"name": "p1"}, {"name": "p2"}])
        fake_board.pin_list = MagicMock(return_value=fake_list)

        app = _build_app()
        client = TestClient(app)

        with patch(
            "apowerb.routers.data_lake.StorageBoardFactory"
        ) as mock_factory_cls:
            mock_factory = MagicMock()
            mock_factory.get_board = MagicMock(return_value=fake_board)
            mock_factory_cls.return_value = mock_factory

            resp = client.post(
                "/api/data-lake/pins/list",
                json={
                    "bucket_name": "alice-bucket",
                    "prefix": "alice/",
                    "storage_source": "s3",
                },
            )

        assert resp.status_code == 200, resp.text
        # Factory instantiated with alice's parameters, not bob's
        mock_factory_cls.assert_called_once_with(
            bucket_name="alice-bucket", prefix="alice/"
        )
        body = resp.json()
        assert body["success"] is True
        assert len(body["pins"]) == 2


# ---------------------------------------------------------------------------
# 3. Write failure on a bucket raises 500 (cross-tenant attempts to write to
#    someone else's bucket get rejected by the storage backend, which the API
#    surfaces as a 500).
# ---------------------------------------------------------------------------


class TestWriteCrossTenantRejected:
    def test_write_to_unauthorized_bucket_returns_500(self):
        """Attempting to write to a bucket the user doesn't own causes the
        underlying storage (S3) to raise — which the router propagates as
        ``500 Internal Server Error`` with the S3 error in the detail."""
        app = _build_app()
        client = TestClient(app)

        with patch(
            "apowerb.routers.data_lake.StorageBoardFactory"
        ) as mock_factory_cls:
            mock_factory = MagicMock()
            mock_board = MagicMock()
            mock_board.pin_write = MagicMock(
                side_effect=PermissionError("Access denied to bucket bob-bucket")
            )
            mock_factory.get_board = MagicMock(return_value=mock_board)
            mock_factory_cls.return_value = mock_factory

            resp = client.post(
                "/api/data-lake/pins/write",
                json={
                    "bucket_name": "bob-bucket",  # not owned
                    "prefix": "bob/",
                    "storage_source": "s3",
                    "pin_name": "attack",
                    "data": [{"x": 1}],
                    "pin_type": "parquet",
                },
            )

        assert resp.status_code == 500, resp.text
        assert "Access denied" in resp.json()["detail"]

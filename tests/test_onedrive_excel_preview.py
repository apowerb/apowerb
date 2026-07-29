"""Unit tests for ``GET /api/onedrivebrowser/excel-preview``.

The endpoint downloads a OneDrive Excel/CSV file through the user's
``microsoft_onedrive`` Integration and returns the first N rows + column
names so the frontend can render a wizard preview before creating a chart.

All Graph API calls are mocked via monkeypatch. No network traffic.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


USER_EMAIL = "preview-user@example.com"
USER_ID = 42
REFRESH_TOKEN = "refresh-token-for-preview-user"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_user(email: str = USER_EMAIL, user_id: int = USER_ID):
    u = MagicMock()
    u.email = email
    u.user_id = user_id
    u.role = "USER"
    return u


def _make_db_mock(refresh_token: str | None):
    """AsyncSession mock whose execute() yields an Integration-like object."""
    async def _execute(stmt):
        integration = None
        if refresh_token is not None:
            integration = MagicMock()
            integration.refresh_token = refresh_token
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=integration)
        return result

    db = MagicMock()
    db.execute = AsyncMock(side_effect=_execute)
    return db


def _build_client(
    *,
    refresh_token: str | None = REFRESH_TOKEN,
    user_email: str = USER_EMAIL,
) -> TestClient:
    from th2agent.routers.onedrive_browser import router
    from th2agent.auth.dependencies import get_current_user
    from th2agent.helpers.database import get_db

    app = FastAPI()
    app.include_router(router)

    async def override_user():
        return _fake_user(user_email)

    db_mock = _make_db_mock(refresh_token)

    async def override_db():
        yield db_mock

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    return TestClient(app)


# ---------------------------------------------------------------------------
# Tests — guard clauses
# ---------------------------------------------------------------------------


class TestExcelPreviewGuards:
    def test_preview_rejects_missing_item_path(self):
        client = _build_client()
        resp = client.get("/api/onedrivebrowser/excel-preview")
        # FastAPI returns 422 for missing required Query param.
        assert resp.status_code in (400, 422)

    def test_preview_rejects_empty_item_path(self):
        client = _build_client()
        resp = client.get(
            "/api/onedrivebrowser/excel-preview",
            params={"item_path": "   "},
        )
        assert resp.status_code in (400, 422)

    def test_preview_rejects_missing_integration(self, monkeypatch):
        """No Integration row → 401 like the other routes in this router."""
        client = _build_client(refresh_token=None)
        resp = client.get(
            "/api/onedrivebrowser/excel-preview",
            params={"item_path": "Reports/data.xlsx"},
        )
        assert resp.status_code == 401
        body = resp.json()
        assert body.get("status") == "error"


# ---------------------------------------------------------------------------
# Tests — happy path
# ---------------------------------------------------------------------------


class TestExcelPreviewHappyPath:
    def _patch_graph(self, monkeypatch, df, err=None):
        """Patch the Graph layer so the route never hits the network."""
        from th2agent.routers import onedrive_browser as odb
        from th2agent.helpers import encryptor as enc_mod

        # Avoid encryptor noise — treat refresh_token as plaintext.
        monkeypatch.setattr(
            enc_mod,
            "decrypt_value",
            lambda v: v,
            raising=True,
        )
        monkeypatch.setattr(
            odb,
            "_graph_headers",
            lambda: {"Authorization": "Bearer fake"},
            raising=True,
        )
        monkeypatch.setattr(
            odb,
            "shared_download_and_parse",
            lambda *a, **kw: (df, err),
            raising=True,
        )

    def test_preview_returns_columns_and_rows(self, monkeypatch):
        df = pd.DataFrame(
            [
                {"Email": "a@b.c", "firstname": "Ann", "Company": "Acme", "Status": "sent"},
                {"Email": "d@e.f", "firstname": "Dan", "Company": "Beta", "Status": "bounced"},
                {"Email": "g@h.i", "firstname": "Gus", "Company": "Gamma", "Status": ""},
            ]
        )
        self._patch_graph(monkeypatch, df)

        client = _build_client()
        resp = client.get(
            "/api/onedrivebrowser/excel-preview",
            params={"item_path": "Reports/data.xlsx"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["columns"] == ["Email", "firstname", "Company", "Status"]
        assert len(body["rows"]) == 3
        assert body["rows"][0] == {
            "Email": "a@b.c",
            "firstname": "Ann",
            "Company": "Acme",
            "Status": "sent",
        }

    def test_preview_respects_limit(self, monkeypatch):
        rows = [{"n": i} for i in range(20)]
        df = pd.DataFrame(rows)
        self._patch_graph(monkeypatch, df)

        client = _build_client()
        resp = client.get(
            "/api/onedrivebrowser/excel-preview",
            params={"item_path": "Reports/data.xlsx", "limit": 5},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["columns"] == ["n"]
        assert len(body["rows"]) == 5
        assert body["rows"][0] == {"n": 0}
        assert body["rows"][-1] == {"n": 4}

    def test_preview_default_limit_is_five(self, monkeypatch):
        rows = [{"n": i} for i in range(20)]
        df = pd.DataFrame(rows)
        self._patch_graph(monkeypatch, df)

        client = _build_client()
        resp = client.get(
            "/api/onedrivebrowser/excel-preview",
            params={"item_path": "Reports/data.xlsx"},
        )
        assert resp.status_code == 200, resp.text
        assert len(resp.json()["rows"]) == 5

    def test_preview_rejects_oversized_limit(self, monkeypatch):
        """limit > 50 should be rejected at validation time (422)."""
        df = pd.DataFrame([{"n": 1}])
        self._patch_graph(monkeypatch, df)

        client = _build_client()
        resp = client.get(
            "/api/onedrivebrowser/excel-preview",
            params={"item_path": "Reports/data.xlsx", "limit": 500},
        )
        assert resp.status_code == 422

    def test_preview_cleans_nan_values(self, monkeypatch):
        """pandas NaN must become '' so the JSON payload is valid."""
        df = pd.DataFrame(
            [
                {"Email": "a@b.c", "Company": "Acme"},
                {"Email": "d@e.f", "Company": None},
            ]
        )
        self._patch_graph(monkeypatch, df)

        client = _build_client()
        resp = client.get(
            "/api/onedrivebrowser/excel-preview",
            params={"item_path": "Reports/data.xlsx"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # NaN → "" — every value must be JSON-serialisable and non-null.
        for row in body["rows"]:
            for value in row.values():
                assert value is not None
        assert body["rows"][1]["Company"] == ""

    def test_preview_forwards_sheet_name(self, monkeypatch):
        """sheet_name query param must reach the parser verbatim."""
        from th2agent.routers import onedrive_browser as odb
        from th2agent.helpers import encryptor as enc_mod

        captured: dict = {}

        def fake_parse(item_path, headers, *, sheet_name=None):
            captured["item_path"] = item_path
            captured["sheet_name"] = sheet_name
            return pd.DataFrame([{"A": 1}]), None

        monkeypatch.setattr(enc_mod, "decrypt_value", lambda v: v, raising=True)
        monkeypatch.setattr(
            odb, "_graph_headers", lambda: {"Authorization": "Bearer fake"}, raising=True
        )
        monkeypatch.setattr(odb, "shared_download_and_parse", fake_parse, raising=True)

        client = _build_client()
        resp = client.get(
            "/api/onedrivebrowser/excel-preview",
            params={
                "item_path": "Reports/data.xlsx",
                "sheet_name": "Feuille1",
            },
        )
        assert resp.status_code == 200, resp.text
        assert captured["item_path"] == "Reports/data.xlsx"
        assert captured["sheet_name"] == "Feuille1"

    def test_preview_forwards_sheet_name_as_int_when_digit(self, monkeypatch):
        """A numeric sheet_name like '1' must be forwarded as int."""
        from th2agent.routers import onedrive_browser as odb
        from th2agent.helpers import encryptor as enc_mod

        captured: dict = {}

        def fake_parse(item_path, headers, *, sheet_name=None):
            captured["sheet_name"] = sheet_name
            return pd.DataFrame([{"A": 1}]), None

        monkeypatch.setattr(enc_mod, "decrypt_value", lambda v: v, raising=True)
        monkeypatch.setattr(
            odb, "_graph_headers", lambda: {"Authorization": "Bearer fake"}, raising=True
        )
        monkeypatch.setattr(odb, "shared_download_and_parse", fake_parse, raising=True)

        client = _build_client()
        resp = client.get(
            "/api/onedrivebrowser/excel-preview",
            params={"item_path": "Reports/data.xlsx", "sheet_name": "1"},
        )
        assert resp.status_code == 200, resp.text
        assert captured["sheet_name"] == 1
        assert isinstance(captured["sheet_name"], int)


# ---------------------------------------------------------------------------
# Tests — error propagation
# ---------------------------------------------------------------------------


class TestExcelPreviewErrors:
    def test_preview_handles_parse_error(self, monkeypatch):
        from th2agent.routers import onedrive_browser as odb
        from th2agent.helpers import encryptor as enc_mod

        monkeypatch.setattr(enc_mod, "decrypt_value", lambda v: v, raising=True)
        monkeypatch.setattr(
            odb, "_graph_headers", lambda: {"Authorization": "Bearer fake"}, raising=True
        )
        monkeypatch.setattr(
            odb,
            "shared_download_and_parse",
            lambda *a, **kw: (None, "bad file"),
            raising=True,
        )

        client = _build_client()
        resp = client.get(
            "/api/onedrivebrowser/excel-preview",
            params={"item_path": "Reports/bad.xlsx"},
        )
        assert resp.status_code == 422
        body = resp.json()
        assert body.get("status") == "error"
        assert "bad file" in body.get("message", "")

    def test_preview_handles_graph_error(self, monkeypatch):
        from th2agent.routers import onedrive_browser as odb
        from th2agent.helpers import encryptor as enc_mod

        monkeypatch.setattr(enc_mod, "decrypt_value", lambda v: v, raising=True)
        monkeypatch.setattr(
            odb, "_graph_headers", lambda: {"Authorization": "Bearer fake"}, raising=True
        )

        def boom(*a, **kw):
            raise RuntimeError("graph exploded")

        monkeypatch.setattr(odb, "shared_download_and_parse", boom, raising=True)

        client = _build_client()
        resp = client.get(
            "/api/onedrivebrowser/excel-preview",
            params={"item_path": "Reports/data.xlsx"},
        )
        assert resp.status_code in (400, 502)
        body = resp.json()
        assert body.get("status") == "error"

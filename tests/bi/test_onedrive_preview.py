"""Unit tests for ``POST /api/v1/bi/onedrive/preview``.

The endpoint lives next to the CSV preview in ``bi/data/dataset_router.py``
so the frontend can share its rendering logic. It returns the same shape:

    {
      "columns":    [{"name": str, "type": str}],
      "row_count":  int,
      "sample_rows": [ {..}, {..}, ... ]
    }

All Graph / DB interactions are mocked — no network traffic.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


USER_EMAIL = "bi-preview@example.com"
USER_ID = 77
REFRESH_TOKEN = "refresh-token-bi"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_user(email: str = USER_EMAIL, user_id: int = USER_ID) -> MagicMock:
    u = MagicMock()
    u.email = email
    u.user_id = user_id
    u.role = "USER"
    return u


def _make_db_mock(refresh_token: str | None):
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
    from apowerb.bi.data.dataset_router import router
    from apowerb.auth.dependencies import get_current_user
    from apowerb.helpers.database import get_db

    app = FastAPI()
    # Mount without prefix here — the tests call /bi/onedrive/preview directly.
    app.include_router(router)

    async def override_user():
        return _fake_user(user_email)

    db_mock = _make_db_mock(refresh_token)

    async def override_db():
        yield db_mock

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    return TestClient(app)


def _patch_onedrive_layer(monkeypatch: pytest.MonkeyPatch, df, err=None) -> None:
    """Stub Graph auth + spreadsheet parser so the route never hits the network."""
    from apowerb.bi.data import dataset_router as dr
    from apowerb.helpers import encryptor as enc_mod

    monkeypatch.setattr(enc_mod, "decrypt_value", lambda v: v, raising=True)
    monkeypatch.setattr(
        dr,
        "_graph_headers",
        lambda: {"Authorization": "Bearer fake"},
        raising=True,
    )
    monkeypatch.setattr(
        dr,
        "download_and_parse_spreadsheet",
        lambda *a, **kw: (df, err),
        raising=True,
    )


# ---------------------------------------------------------------------------
# Tests — happy path
# ---------------------------------------------------------------------------


class TestOnedrivePreviewHappyPath:
    def test_preview_xlsx_returns_csv_like_shape(self, monkeypatch: pytest.MonkeyPatch) -> None:
        df = pd.DataFrame(
            [
                {"Email": "a@b.c", "Age": 30},
                {"Email": "d@e.f", "Age": 25},
                {"Email": "g@h.i", "Age": 40},
            ]
        )
        _patch_onedrive_layer(monkeypatch, df)

        client = _build_client()
        resp = client.post(
            "/bi/onedrive/preview",
            json={"item_path": "Reports/data.xlsx"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()

        # Columns use the {name, type} shape identical to the CSV preview.
        assert isinstance(body["columns"], list)
        assert body["columns"][0]["name"] == "Email"
        assert {c["name"] for c in body["columns"]} == {"Email", "Age"}
        # Full row count (not just sample size).
        assert body["row_count"] == 3
        # sample_rows are dicts keyed by column name.
        assert len(body["sample_rows"]) == 3
        assert body["sample_rows"][0]["Email"] == "a@b.c"

    def test_preview_csv_returns_rows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        df = pd.DataFrame(
            [
                {"col": f"v{i}"} for i in range(4)
            ]
        )
        _patch_onedrive_layer(monkeypatch, df)

        client = _build_client()
        resp = client.post(
            "/bi/onedrive/preview",
            json={"item_path": "Reports/data.csv"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["row_count"] == 4
        assert body["columns"][0]["name"] == "col"
        assert body["sample_rows"][0]["col"] == "v0"

    def test_preview_caps_sample_rows_at_ten(self, monkeypatch: pytest.MonkeyPatch) -> None:
        df = pd.DataFrame([{"n": i} for i in range(50)])
        _patch_onedrive_layer(monkeypatch, df)

        client = _build_client()
        resp = client.post(
            "/bi/onedrive/preview",
            json={"item_path": "Reports/data.xlsx"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # row_count reports the full dataframe size; sample_rows is capped at 10.
        assert body["row_count"] == 50
        assert len(body["sample_rows"]) == 10

    def test_preview_forwards_sheet_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from apowerb.bi.data import dataset_router as dr
        from apowerb.helpers import encryptor as enc_mod

        captured: dict = {}

        def fake_parse(item_path, headers, *, sheet_name=None):
            captured["item_path"] = item_path
            captured["sheet_name"] = sheet_name
            return pd.DataFrame([{"A": 1}]), None

        monkeypatch.setattr(enc_mod, "decrypt_value", lambda v: v, raising=True)
        monkeypatch.setattr(
            dr, "_graph_headers", lambda: {"Authorization": "Bearer fake"}, raising=True
        )
        monkeypatch.setattr(
            dr, "download_and_parse_spreadsheet", fake_parse, raising=True
        )

        client = _build_client()
        resp = client.post(
            "/bi/onedrive/preview",
            json={
                "item_path": "Reports/multi.xlsx",
                "sheet_name": "Sheet2",
            },
        )
        assert resp.status_code == 200, resp.text
        assert captured["item_path"] == "Reports/multi.xlsx"
        assert captured["sheet_name"] == "Sheet2"


# ---------------------------------------------------------------------------
# Tests — errors
# ---------------------------------------------------------------------------


class TestOnedrivePreviewErrors:
    def test_missing_integration_returns_401(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from apowerb.helpers import encryptor as enc_mod

        monkeypatch.setattr(enc_mod, "decrypt_value", lambda v: v, raising=True)

        client = _build_client(refresh_token=None)
        resp = client.post(
            "/bi/onedrive/preview",
            json={"item_path": "Reports/data.xlsx"},
        )
        assert resp.status_code == 401
        body = resp.json()
        assert body.get("status") == "error"

    def test_unsupported_extension_returns_415(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Simulate the parser rejecting an unsupported format.
        _patch_onedrive_layer(
            monkeypatch,
            None,
            err="Unsupported spreadsheet format: '.parquet'. Expected one of: .csv, .ods, .tsv, .xls, .xlsm, .xlsx.",
        )
        client = _build_client()
        resp = client.post(
            "/bi/onedrive/preview",
            json={"item_path": "Reports/data.parquet"},
        )
        assert resp.status_code == 415
        body = resp.json()
        assert body.get("status") == "error"
        assert "parquet" in body.get("message", "").lower() or "unsupported" in body.get("message", "").lower()

    def test_file_not_found_returns_404(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_onedrive_layer(
            monkeypatch,
            None,
            err="Download failed (HTTP 404): itemNotFound",
        )
        client = _build_client()
        resp = client.post(
            "/bi/onedrive/preview",
            json={"item_path": "Reports/missing.xlsx"},
        )
        assert resp.status_code == 404
        body = resp.json()
        assert body.get("status") == "error"

    def test_missing_item_path_returns_400(self) -> None:
        # Spec: empty body (neither item_path nor item_id) → 400, not 422
        # because both fields are individually optional but one is required.
        client = _build_client()
        resp = client.post("/bi/onedrive/preview", json={})
        assert resp.status_code == 400

    def test_empty_item_path_is_rejected(self) -> None:
        client = _build_client()
        resp = client.post(
            "/bi/onedrive/preview",
            json={"item_path": "   "},
        )
        assert resp.status_code in (400, 422)

    def test_generic_parse_error_returns_500(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_onedrive_layer(
            monkeypatch,
            None,
            err="Failed to parse spreadsheet (.xlsx): corrupt file",
        )
        client = _build_client()
        resp = client.post(
            "/bi/onedrive/preview",
            json={"item_path": "Reports/corrupt.xlsx"},
        )
        assert resp.status_code == 500
        body = resp.json()
        assert body.get("status") == "error"
        assert "corrupt" in body.get("message", "").lower() or "parse" in body.get("message", "").lower()

    def test_missing_path_and_id_returns_400(self) -> None:
        """Body with neither item_path nor item_id is rejected."""
        client = _build_client()
        resp = client.post(
            "/bi/onedrive/preview",
            json={"sheet_name": "Sheet1"},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body.get("status") == "error"
        assert "item_path" in body.get("message", "").lower() or "item_id" in body.get("message", "").lower()


# ---------------------------------------------------------------------------
# Tests — response includes sheet_name + item_id fallback + int sheet index
# ---------------------------------------------------------------------------


class TestOnedrivePreviewResponseShape:
    def test_response_includes_sheet_name_when_provided(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        df = pd.DataFrame([{"A": 1}])
        _patch_onedrive_layer(monkeypatch, df)
        client = _build_client()
        resp = client.post(
            "/bi/onedrive/preview",
            json={
                "item_path": "Reports/multi.xlsx",
                "sheet_name": "Sheet2",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "sheet_name" in body
        assert body["sheet_name"] == "Sheet2"

    def test_response_sheet_name_is_none_for_csv(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        df = pd.DataFrame([{"A": 1}])
        _patch_onedrive_layer(monkeypatch, df)
        client = _build_client()
        resp = client.post(
            "/bi/onedrive/preview",
            json={"item_path": "Reports/data.csv"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "sheet_name" in body
        assert body["sheet_name"] is None

    def test_response_sheet_name_is_none_for_tsv(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        df = pd.DataFrame([{"A": 1}])
        _patch_onedrive_layer(monkeypatch, df)
        client = _build_client()
        resp = client.post(
            "/bi/onedrive/preview",
            json={"item_path": "Reports/data.tsv"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["sheet_name"] is None

    def test_sheet_name_accepts_integer_index(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from apowerb.bi.data import dataset_router as dr
        from apowerb.helpers import encryptor as enc_mod

        captured: dict = {}

        def fake_parse(item_path, headers, *, sheet_name=None):
            captured["sheet_name"] = sheet_name
            return pd.DataFrame([{"A": 1}]), None

        monkeypatch.setattr(enc_mod, "decrypt_value", lambda v: v, raising=True)
        monkeypatch.setattr(
            dr, "_graph_headers", lambda: {"Authorization": "Bearer fake"}, raising=True
        )
        monkeypatch.setattr(
            dr, "download_and_parse_spreadsheet", fake_parse, raising=True
        )

        client = _build_client()
        resp = client.post(
            "/bi/onedrive/preview",
            json={"item_path": "Reports/x.xlsx", "sheet_name": 2},
        )
        assert resp.status_code == 200, resp.text
        assert captured["sheet_name"] == 2


class TestOnedrivePreviewItemIdFallback:
    def test_item_id_resolves_to_path_when_item_path_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from apowerb.bi.data import dataset_router as dr
        from apowerb.helpers import encryptor as enc_mod

        captured: dict = {}

        def fake_parse(item_path, headers, *, sheet_name=None):
            captured["item_path"] = item_path
            return pd.DataFrame([{"A": 1}]), None

        def fake_resolve(item_id, headers):
            captured["resolved_item_id"] = item_id
            return "Resolved/Path/file.xlsx"

        monkeypatch.setattr(enc_mod, "decrypt_value", lambda v: v, raising=True)
        monkeypatch.setattr(
            dr, "_graph_headers", lambda: {"Authorization": "Bearer fake"}, raising=True
        )
        monkeypatch.setattr(
            dr, "download_and_parse_spreadsheet", fake_parse, raising=True
        )
        monkeypatch.setattr(
            dr, "_resolve_path_from_item_id", fake_resolve, raising=True
        )

        client = _build_client()
        resp = client.post(
            "/bi/onedrive/preview",
            json={"item_id": "01ABCDEF"},
        )
        assert resp.status_code == 200, resp.text
        assert captured["resolved_item_id"] == "01ABCDEF"
        assert captured["item_path"] == "Resolved/Path/file.xlsx"

    def test_item_path_takes_precedence_over_item_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from apowerb.bi.data import dataset_router as dr
        from apowerb.helpers import encryptor as enc_mod

        captured: dict = {}

        def fake_parse(item_path, headers, *, sheet_name=None):
            captured["item_path"] = item_path
            return pd.DataFrame([{"A": 1}]), None

        def fake_resolve(item_id, headers):
            raise AssertionError("should not be called when item_path is provided")

        monkeypatch.setattr(enc_mod, "decrypt_value", lambda v: v, raising=True)
        monkeypatch.setattr(
            dr, "_graph_headers", lambda: {"Authorization": "Bearer fake"}, raising=True
        )
        monkeypatch.setattr(
            dr, "download_and_parse_spreadsheet", fake_parse, raising=True
        )
        monkeypatch.setattr(
            dr, "_resolve_path_from_item_id", fake_resolve, raising=True
        )

        client = _build_client()
        resp = client.post(
            "/bi/onedrive/preview",
            json={"item_path": "Direct/path.xlsx", "item_id": "ignored"},
        )
        assert resp.status_code == 200, resp.text
        assert captured["item_path"] == "Direct/path.xlsx"

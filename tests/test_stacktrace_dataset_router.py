"""py/stack-trace-exposure regression tests for bi/data/dataset_router.py.

POST /bi/onedrive/preview had three ``except RuntimeError as exc`` branches
that forwarded ``str(exc)`` (or a local ``msg = str(exc)``) verbatim to the
client. The third branch also derives its HTTP status code (404 vs 500)
from the exception text — that classification logic must survive the fix,
only the text shown to the client changes.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

SENTINEL = "s3://internal-bucket/secret-path-leak-42"


@pytest.fixture()
def client():
    from apowerb.auth.dependencies import get_current_user
    from apowerb.bi.data.dataset_router import router
    from apowerb.helpers.database import get_db

    app = FastAPI()
    app.include_router(router)

    async def override_user():
        u = MagicMock()
        u.user_id = 1
        u.email = "alice@example.com"
        return u

    async def override_db():
        yield MagicMock()

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db

    return TestClient(app)


MODULE = "apowerb.bi.data.dataset_router"


class TestOnedrivePreviewRuntimeErrorSites:
    """Sites 339, 362, 387: ``except RuntimeError as exc``."""

    def test_resolve_token_failure_hides_exception_text(self, client):
        with patch(
            f"{MODULE}._resolve_onedrive_refresh_token",
            new_callable=AsyncMock,
            side_effect=RuntimeError(SENTINEL),
        ):
            resp = client.post(
                "/bi/onedrive/preview", json={"item_path": "Documents/x.xlsx"}
            )

        assert resp.status_code == 401
        assert SENTINEL not in resp.text
        assert resp.json()["message"]

    def test_graph_headers_failure_hides_exception_text(self, client):
        with patch(
            f"{MODULE}._resolve_onedrive_refresh_token",
            new_callable=AsyncMock,
            return_value="fake-refresh-token",
        ), patch(f"{MODULE}._graph_headers", side_effect=RuntimeError(SENTINEL)):
            resp = client.post(
                "/bi/onedrive/preview", json={"item_path": "Documents/x.xlsx"}
            )

        assert resp.status_code == 401
        assert SENTINEL not in resp.text

    def test_item_id_resolution_failure_hides_exception_text_and_keeps_500(self, client):
        with patch(
            f"{MODULE}._resolve_onedrive_refresh_token",
            new_callable=AsyncMock,
            return_value="fake-refresh-token",
        ), patch(f"{MODULE}._graph_headers", return_value={}), patch(
            f"{MODULE}._resolve_path_from_item_id",
            side_effect=RuntimeError(SENTINEL),
        ):
            resp = client.post("/bi/onedrive/preview", json={"item_id": "abc123"})

        assert resp.status_code == 500
        assert SENTINEL not in resp.text

    def test_item_id_resolution_404_failure_hides_exception_text_and_keeps_404(self, client):
        with patch(
            f"{MODULE}._resolve_onedrive_refresh_token",
            new_callable=AsyncMock,
            return_value="fake-refresh-token",
        ), patch(f"{MODULE}._graph_headers", return_value={}), patch(
            f"{MODULE}._resolve_path_from_item_id",
            side_effect=RuntimeError(f"Graph API returned 404: {SENTINEL}"),
        ):
            resp = client.post("/bi/onedrive/preview", json={"item_id": "abc123"})

        assert resp.status_code == 404
        assert SENTINEL not in resp.text

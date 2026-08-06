"""py/stack-trace-exposure regression tests for routers/google_drive_browser.py.

Mirrors tests/test_stacktrace_onedrive_browser.py: the ``except RuntimeError``
branches used to forward ``str(exc)`` verbatim, and /list & /search forwarded
the raw dict from tool_list_files/tool_search_files (which can itself embed
exception text). Both are fixed via apowerb.helpers.error_responses.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

SENTINEL = "GOOGLE_DRIVE_CLIENT_SECRET-leak-abc987"


@pytest.fixture()
def client():
    from apowerb.auth.dependencies import get_current_user
    from apowerb.helpers.database import get_db
    from apowerb.routers.google_drive_browser import router

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


class TestResolveTokenRuntimeErrorSites:
    """Sites 92, 118, 147, 160: ``except RuntimeError as exc``."""

    def test_list_files_hides_exception_text(self, client):
        with patch(
            "apowerb.routers.google_drive_browser._resolve_google_drive_refresh_token",
            new_callable=AsyncMock,
            side_effect=RuntimeError(SENTINEL),
        ):
            resp = client.get("/api/googledrivebrowser/list")

        assert resp.status_code == 401
        assert SENTINEL not in resp.text
        body = resp.json()
        assert body["status"] == "error"
        assert body["message"]

    def test_search_files_hides_exception_text(self, client):
        with patch(
            "apowerb.routers.google_drive_browser._resolve_google_drive_refresh_token",
            new_callable=AsyncMock,
            side_effect=RuntimeError(SENTINEL),
        ):
            resp = client.get("/api/googledrivebrowser/search", params={"q": "report"})

        assert resp.status_code == 401
        assert SENTINEL not in resp.text

    def test_get_file_content_hides_exception_text_on_resolve_token(self, client):
        with patch(
            "apowerb.routers.google_drive_browser._resolve_google_drive_refresh_token",
            new_callable=AsyncMock,
            side_effect=RuntimeError(SENTINEL),
        ):
            resp = client.get("/api/googledrivebrowser/content", params={"file_id": "abc"})

        assert resp.status_code == 401
        assert SENTINEL not in resp.text

    def test_get_file_content_hides_exception_text_on_auth_headers(self, client):
        with patch(
            "apowerb.routers.google_drive_browser._resolve_google_drive_refresh_token",
            new_callable=AsyncMock,
            return_value="fake-refresh-token",
        ), patch(
            "apowerb.routers.google_drive_browser.google_auth_headers",
            side_effect=RuntimeError(SENTINEL),
        ):
            resp = client.get("/api/googledrivebrowser/content", params={"file_id": "abc"})

        assert resp.status_code == 401
        assert SENTINEL not in resp.text


class TestToolErrorForwardingSites:
    """Sites 98, 124: routes forwarding tool_list_files/tool_search_files
    error dicts verbatim."""

    def test_list_files_sanitizes_tool_error_message(self, client):
        tool_result = {
            "status": "error",
            "message": f"Failed to list files: {SENTINEL}. Do not retry.",
            "retry": False,
        }
        with patch(
            "apowerb.routers.google_drive_browser._resolve_google_drive_refresh_token",
            new_callable=AsyncMock,
            return_value="fake-refresh-token",
        ), patch(
            "apowerb.routers.google_drive_browser.tool_list_files",
            return_value=tool_result,
        ):
            resp = client.get("/api/googledrivebrowser/list")

        assert resp.status_code == 200
        assert SENTINEL not in resp.text
        body = resp.json()
        assert body["status"] == "error"
        assert body["retry"] is False
        assert body["message"] != tool_result["message"]

    def test_search_files_sanitizes_tool_error_message(self, client):
        tool_result = {
            "status": "error",
            "message": f"Failed to search files: {SENTINEL}. Do not retry.",
            "retry": False,
        }
        with patch(
            "apowerb.routers.google_drive_browser._resolve_google_drive_refresh_token",
            new_callable=AsyncMock,
            return_value="fake-refresh-token",
        ), patch(
            "apowerb.routers.google_drive_browser.tool_search_files",
            return_value=tool_result,
        ):
            resp = client.get("/api/googledrivebrowser/search", params={"q": "report"})

        assert resp.status_code == 200
        assert SENTINEL not in resp.text
        body = resp.json()
        assert body["status"] == "error"
        assert body["message"] != tool_result["message"]

    def test_list_files_success_result_passes_through_unchanged(self, client):
        tool_result = {"status": "success", "items": [], "total": 0}
        with patch(
            "apowerb.routers.google_drive_browser._resolve_google_drive_refresh_token",
            new_callable=AsyncMock,
            return_value="fake-refresh-token",
        ), patch(
            "apowerb.routers.google_drive_browser.tool_list_files",
            return_value=tool_result,
        ):
            resp = client.get("/api/googledrivebrowser/list")

        assert resp.status_code == 200
        assert resp.json() == tool_result

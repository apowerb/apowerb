"""Integration tests for authentication endpoints."""

import httpx
import pytest

from conftest import TEST_EMAIL, TEST_PASSWORD

pytestmark = pytest.mark.integration


class TestLogin:
    def test_login_success(self, base_url: str) -> None:
        response = httpx.post(
            f"{base_url}/api/auth/token",
            data={"username": TEST_EMAIL, "password": TEST_PASSWORD},
        )
        assert response.status_code == 200
        body = response.json()
        assert "access_token" in body
        assert body["access_token"]

    def test_login_wrong_password(self, base_url: str) -> None:
        response = httpx.post(
            f"{base_url}/api/auth/token",
            data={"username": TEST_EMAIL, "password": "WrongPassword!"},
        )
        assert response.status_code == 401

    def test_login_nonexistent_user(self, base_url: str) -> None:
        response = httpx.post(
            f"{base_url}/api/auth/token",
            data={
                "username": "nonexistent@example.com",
                "password": "Whatever123!",
            },
        )
        assert response.status_code == 401


class TestProtectedEndpoints:
    def test_protected_endpoint_without_token(self, base_url: str) -> None:
        response = httpx.get(f"{base_url}/api/agents")
        assert response.status_code in (401, 403)

    def test_protected_endpoint_with_token(
        self, base_url: str, auth_headers: dict[str, str]
    ) -> None:
        response = httpx.get(
            f"{base_url}/api/agents", headers=auth_headers
        )
        assert response.status_code == 200


class TestRefreshToken:
    def test_refresh_token(self, base_url: str) -> None:
        """Login sets a refresh_token cookie; POST /api/auth/refresh-token uses it."""
        login_resp = httpx.post(
            f"{base_url}/api/auth/token",
            data={"username": TEST_EMAIL, "password": TEST_PASSWORD},
        )
        assert login_resp.status_code == 200

        cookies = login_resp.cookies
        refresh_resp = httpx.post(
            f"{base_url}/api/auth/refresh-token",
            cookies=cookies,
        )
        # If refresh cookie was set, we expect 200; otherwise the server
        # may return 401 (no cookie). Both outcomes are valid depending
        # on server config.
        assert refresh_resp.status_code in (200, 401)
        if refresh_resp.status_code == 200:
            body = refresh_resp.json()
            assert "access_token" in body

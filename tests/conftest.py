"""Shared fixtures for integration tests.

These fixtures target a running th2agent server on localhost:8000.
They are NOT meant for unit tests — unit tests should remain isolated.
"""

import os

import httpx
import pytest

TEST_EMAIL = os.environ.get("E2E_TEST_EMAIL", "e2e-test@th2ai.com")
# E2E_TEST_PASSWORD is only required for the integration fixtures below.
# Leaving it optional so unit tests (which don't hit a running server) can run
# without setting it.
TEST_PASSWORD = os.environ.get("E2E_TEST_PASSWORD", "")


@pytest.fixture(scope="session")
def base_url() -> str:
    return "http://localhost:8000"


@pytest.fixture(scope="session")
def auth_token(base_url: str) -> str:
    """Authenticate once per session and return the access_token."""
    response = httpx.post(
        f"{base_url}/api/auth/token",
        data={"username": TEST_EMAIL, "password": TEST_PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 200, (
        f"Login failed ({response.status_code}): {response.text}"
    )
    body = response.json()
    token = body.get("access_token")
    assert token, f"No access_token in response: {body}"
    return token


@pytest.fixture(scope="session")
def auth_headers(auth_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {auth_token}"}


@pytest.fixture()
def created_agent_ids(
    base_url: str, auth_headers: dict[str, str]
) -> list[str]:
    """Track agent IDs created during a test; delete them on teardown."""
    ids: list[str] = []
    yield ids
    for agent_id in ids:
        httpx.delete(
            f"{base_url}/api/agents/{agent_id}",
            headers=auth_headers,
        )

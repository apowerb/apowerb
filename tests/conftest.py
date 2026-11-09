"""Shared fixtures for integration tests.

These fixtures target a running th2agent server on localhost:8000.
They are NOT meant for unit tests — unit tests should remain isolated.
"""

import os
from contextlib import asynccontextmanager

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


# Backported from main for the 0.1.21.post6 tests: an in-memory SQLite
# database shaped like the Postgres one (ATTACH keeps the schema-qualified
# metadata as production has it).
@pytest.fixture
async def sqlite_engine():
    """In-memory SQLite engine holding every table of ``Base.metadata``.

    One connection (StaticPool): an attached database lives and dies with
    the connection that attached it.
    """
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import StaticPool

    from apowerb.helpers.database import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    schemas = sorted({t.schema for t in Base.metadata.tables.values() if t.schema})
    async with engine.begin() as conn:
        for schema in schemas:
            await conn.exec_driver_sql(f"ATTACH DATABASE ':memory:' AS \"{schema}\"")
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
def bind_sessionmanager(monkeypatch):
    """Route ``sessionmanager.session()`` to a session factory for one test.

    Every module imports the same ``helpers.database.sessionmanager``, so a
    single patch covers the scheduler, the routers and the guards alike.
    """
    from apowerb.helpers.database import sessionmanager

    def _bind(factory):
        @asynccontextmanager
        async def _session():
            session = factory()
            try:
                yield session
            finally:
                await session.close()

        monkeypatch.setattr(sessionmanager, "session", _session, raising=False)

    return _bind


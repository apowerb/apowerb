"""Shared fixtures.

The first group targets a running th2agent server on localhost:8000 and is
for integration tests only. The second gives unit tests an in-memory SQLite
database shaped like the Postgres one, without touching the models.
"""

import os
import sys
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


# ── SQLite in place of Postgres, schema included ────────────────────────────
# The models are qualified with ``settings.db_schema``: "public" by default,
# empty in CI (DB_SCHEMA=""). SQLite only knows ``main``, so ``public.user``
# fails with "unknown database public". Setting ``Table.schema = None`` to get
# past it rewrites the shared metadata for the rest of the session: every
# later module then creates unqualified tables while its raw SQL still says
# ``public.x``, and the suite passes or fails with file order. ATTACH gives
# SQLite the schema instead, and the metadata stays as production has it.


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


@pytest.fixture(autouse=True)
def _models_schema_untouched():
    """Fail the test that rewrites the models' schema, and undo the change.

    Such a rewrite does not break the test that makes it; it breaks whichever
    module happens to run next. Catching it here names the culprit.
    """
    database = sys.modules.get("apowerb.helpers.database")
    if database is None:
        yield
        return
    metadata = database.Base.metadata
    schema = metadata.schema
    tables = {key: table.schema for key, table in metadata.tables.items()}
    yield
    changed = sorted(
        key for key, table in metadata.tables.items() if table.schema != tables.get(key, table.schema)
    )
    if metadata.schema == schema and not changed:
        return
    metadata.schema = schema
    for key in changed:
        metadata.tables[key].schema = tables[key]
    pytest.fail(
        "this test rewrote the schema of the shared models "
        f"(metadata.schema={schema!r}, tables: {', '.join(changed) or 'none'}); "
        "use the sqlite_engine fixture instead",
        pytrace=False,
    )


@pytest.fixture(autouse=True)
def _no_decision_notice_leaves(monkeypatch):
    """No human-decision notice (mail, Teams) ever leaves a test.

    A run that pauses on an ``approval`` node announces itself from the very
    stream the test drains; the tests that check those notices install their
    own recorder over this one.
    """
    from apowerb.core import decision_notices

    async def _nowhere(**kwargs):
        return None

    monkeypatch.setattr(decision_notices, "_send_mail", _nowhere)
    monkeypatch.setattr(decision_notices, "_send_teams", _nowhere)


@pytest.fixture
def demo_client_template(monkeypatch):
    """A client-restricted SuperAgent template, registered as an overlay would.

    The core ships no client template. Tests that need one (per-org
    visibility, drift detection) get this fake, visible to the ``acme`` org
    only. It is returned as the very dict the catalog serves, so a test can
    mutate it with ``monkeypatch.setitem``.
    """
    from apowerb.core.extensions.registry import registry

    template = {
        "template_id": "demo_client_assistant",
        "name": "demo_client_assistant",
        "display_name": "Demo client assistant",
        "agent_description": "Reads supplier acknowledgements for one client.",
        "agent_instruction": "Read each acknowledgement and record it.",
        "agent_tools": [
            "outlook_mail.tool_list_emails",
            "basic.tool_pdf_to_images",
        ],
        "tags": ["demo"],
        "visible_to_orgs": ["acme"],
    }
    registered = registry.templates
    monkeypatch.setattr(registry, "templates", lambda: [*registered(), template])
    return template

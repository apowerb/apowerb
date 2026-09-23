"""GET /conversations/share — the caller's active shared snapshots.

Runs against a real database (SQLite in memory): the guarantee is the
query's filters, which the dict-backed fake of ``test_share_auth.py``
cannot see. Forgetting one would list someone else's links, or a link
that no longer leads anywhere.
"""
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from apowerb.auth.dependencies import get_current_user
from apowerb.helpers.database import Base, get_db
from apowerb.models import SharedConversation
from apowerb.routers import share as share_module

ALICE = "alice@example.com"
BOB = "bob@example.com"
FRONT = "https://app.example.com"


class _FakeSettings:
    app_public_url = FRONT + "/"


def _user(email: str):
    u = MagicMock()
    u.email = email
    u.role = "USER"
    return u


@pytest.fixture
async def factory(monkeypatch) -> AsyncIterator:
    monkeypatch.setattr(share_module, "get_settings", lambda: _FakeSettings())
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    schema = SharedConversation.__table__.schema
    async with engine.begin() as conn:
        if schema:
            await conn.execute(text(f"ATTACH DATABASE ':memory:' AS {schema}"))
        await conn.run_sync(
            Base.metadata.create_all, tables=[SharedConversation.__table__]
        )
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _client(factory, email: str | None) -> AsyncClient:
    app = FastAPI()
    app.include_router(share_module.router, prefix="/api")

    async def _db():
        async with factory() as session:
            yield session

    async def _current():
        if email is None:
            from fastapi import HTTPException

            raise HTTPException(status_code=401, detail="Not authenticated")
        return _user(email)

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = _current
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _insert(factory, share_id, owner, *, title="t", days_ago=0, expires_in=30):
    now = datetime.now(timezone.utc)
    async with factory() as session:
        session.add(
            SharedConversation(
                id=share_id,
                title=title,
                agent_name="Assistant",
                messages=[{"role": "user", "content": "hi"}],
                created_at=now - timedelta(days=days_ago),
                expires_at=now + timedelta(days=expires_in),
                owner_id=owner,
                is_public=True,
            )
        )
        await session.commit()


async def test_lists_my_active_shares_newest_first_with_public_url(factory):
    await _insert(factory, "old", ALICE, title="Older", days_ago=3)
    await _insert(factory, "new", ALICE, title="Newer", days_ago=0)

    async with _client(factory, ALICE) as client:
        resp = await client.get("/api/conversations/share")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [s["id"] for s in body] == ["new", "old"]
    assert body[0]["title"] == "Newer"
    assert body[0]["url"] == f"{FRONT}/share/new"
    assert body[0]["isPublic"] is True
    assert body[0]["createdAt"] and body[0]["expiresAt"]


async def test_hides_other_users_revoked_and_expired_shares(factory):
    await _insert(factory, "mine", ALICE)
    await _insert(factory, "bobs", BOB)
    await _insert(factory, "expired", ALICE, days_ago=31, expires_in=-1)
    await _insert(factory, "revoked", ALICE)

    async with _client(factory, ALICE) as client:
        revoke = await client.delete("/api/conversations/share/revoked")
        assert revoke.status_code == 204, revoke.text
        resp = await client.get("/api/conversations/share")

    assert resp.status_code == 200, resp.text
    assert [s["id"] for s in resp.json()] == ["mine"]

    async with _client(factory, BOB) as client:
        resp = await client.get("/api/conversations/share")
    assert [s["id"] for s in resp.json()] == ["bobs"]


async def test_requires_authentication(factory):
    await _insert(factory, "mine", ALICE)
    async with _client(factory, None) as client:
        resp = await client.get("/api/conversations/share")
    assert resp.status_code == 401

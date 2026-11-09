"""Deleting a subscription must not delete its execution history.

Live incident 2026-11-09 (client production): a subscription row was deleted
and ``webhook_logs.subscription_id`` was ``ON DELETE CASCADE`` — the whole
history of the agent (11 179 rows) went with it and had to be restored from a
database backup. The ``DELETE /subscriptions/{id}`` route also deleted the
logs explicitly (roadmap#105).

The logs now outlive their subscription: the foreign key is ``ON DELETE SET
NULL`` (nullable column) and the route detaches the logs instead of deleting
them, independently of the live constraint.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from apowerb.models import User, WebhookLog, WebhookSubscription
from apowerb.routers import webhooks as webhooks_router


@pytest.fixture
async def factory(sqlite_engine, bind_sessionmanager):
    # SQLite only enforces foreign keys (and their ON DELETE action) when asked.
    @event.listens_for(sqlite_engine.sync_engine, "connect")
    def _fk_on(dbapi_conn, _record):  # pragma: no cover - new connections only
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    async with sqlite_engine.connect() as conn:
        await conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    f = async_sessionmaker(sqlite_engine, expire_on_commit=True)
    bind_sessionmanager(f)
    yield f


async def _seed(factory) -> int:
    async with factory() as db:
        db.add(User(user_id=1, first_name="Com", last_name="Client",
                    email="ops@acme.example", password="x", role="USER"))
        await db.commit()
        sub = WebhookSubscription(
            user_id=1, provider="microsoft_outlook",
            subscription_id=None,  # no Graph call in the delete route
            resource="me/mailFolders('Inbox')/messages", change_type="created,updated",
            client_state="state", notification_url="https://example.com/cb",
            status="expired", agent_id=12,
        )
        db.add(sub)
        await db.flush()
        for i in range(2):
            db.add(WebhookLog(user_id=1, subscription_id=sub.id, agent_id=12,
                              trigger_event="created", resource_id=f"Users/u/Messages/{i}",
                              status="success"))
        sub_id = sub.id
        await db.commit()
        return sub_id


async def _logs(factory):
    async with factory() as db:
        return (await db.execute(select(WebhookLog.subscription_id))).scalars().all()


@pytest.mark.asyncio
async def test_deleting_the_subscription_row_keeps_its_logs(factory):
    sub_id = await _seed(factory)

    async with factory() as db:
        await db.execute(text("PRAGMA foreign_keys=ON"))
        await db.execute(
            WebhookSubscription.__table__.delete().where(WebhookSubscription.id == sub_id)
        )
        await db.commit()

    assert await _logs(factory) == [None, None]


@pytest.mark.asyncio
async def test_delete_route_keeps_the_logs_detached(factory):
    sub_id = await _seed(factory)

    async with factory() as db:
        result = await webhooks_router.delete_subscription(
            subscription_db_id=sub_id, db=db, current_user=SimpleNamespace(user_id=1)
        )
    assert result["success"] is True

    async with factory() as db:
        remaining = await db.scalar(
            select(func.count()).select_from(WebhookSubscription)
            .where(WebhookSubscription.id == sub_id)
        )
    assert remaining == 0
    assert await _logs(factory) == [None, None]

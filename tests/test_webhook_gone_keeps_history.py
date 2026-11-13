"""A subscription that Microsoft Graph no longer knows must not be deleted.

Live incident 2026-11-09 (client production): the renewal of an
already-expired subscription answered 404, the local row was deleted, and
``webhook_logs.subscription_id`` is ``ON DELETE CASCADE`` — the whole
execution history of the agent (11 179 rows) went with it and had to be
restored from a database backup.

On a 404 the row is kept and flipped to ``status='expired'``: the history
stays attached, the operator sees it in the UI and creates a new
subscription. Both the renewal cron and the manual renew endpoint are
covered.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from apowerb.models import User, WebhookSubscription
from apowerb.routers import webhooks as webhooks_router
from apowerb.scheduler import webhook_renewal


@pytest.fixture
async def factory(sqlite_engine, bind_sessionmanager):
    f = async_sessionmaker(sqlite_engine, expire_on_commit=True)
    bind_sessionmanager(f)
    yield f


async def _seed(factory, expires_in: timedelta) -> int:
    async with factory() as db:
        db.add(User(user_id=1, first_name="Com", last_name="Client",
                    email="ops@acme.example", password="x", role="USER"))
        await db.commit()
        sub = WebhookSubscription(
            user_id=1, provider="microsoft_outlook",
            subscription_id="4340b0ef-0000-0000-0000-000000000000",
            resource="me/mailFolders('Inbox')/messages", change_type="created,updated",
            client_state="state", notification_url="https://example.com/cb",
            status="active", agent_id=12,
            expiration_datetime=datetime.now(timezone.utc) + expires_in,
        )
        db.add(sub)
        await db.flush()
        sub_id = sub.id
        await db.commit()
        return sub_id


async def _row(factory, sub_id: int):
    async with factory() as db:
        return (await db.execute(
            select(WebhookSubscription).where(WebhookSubscription.id == sub_id)
        )).scalar_one_or_none()


async def _token(db, user_id):
    await db.commit()  # the real helper rotates the refresh token and commits
    return "fake-access-token"


async def _graph_404(access_token, graph_sub_id):
    raise LookupError("Graph subscription not found")


def _patch(monkeypatch, module):
    monkeypatch.setattr(module.OutlookWebhookService, "get_access_token_for_user", _token)
    monkeypatch.setattr(module.OutlookWebhookService, "renew_subscription", _graph_404)


@pytest.mark.asyncio
async def test_cron_keeps_the_row_when_graph_answers_404(factory, monkeypatch):
    sub_id = await _seed(factory, expires_in=timedelta(hours=6))
    _patch(monkeypatch, webhook_renewal)

    await webhook_renewal._renew_expiring_subscriptions()

    row = await _read_or_fail(factory, sub_id)
    assert row.status == "expired"


@pytest.mark.asyncio
async def test_manual_renew_keeps_the_row_when_graph_answers_404(factory, monkeypatch):
    sub_id = await _seed(factory, expires_in=timedelta(hours=-6))
    _patch(monkeypatch, webhooks_router)

    async with factory() as db:
        with pytest.raises(HTTPException) as exc:
            await webhooks_router.renew_subscription(
                subscription_db_id=sub_id, db=db, current_user=SimpleNamespace(user_id=1)
            )
    assert exc.value.status_code == 410

    row = await _read_or_fail(factory, sub_id)
    assert row.status == "expired"


async def _read_or_fail(factory, sub_id):
    row = await _row(factory, sub_id)
    assert row is not None, (
        "the subscription row was deleted: webhook_logs cascades on it, "
        "so the whole execution history would be deleted too"
    )
    return row

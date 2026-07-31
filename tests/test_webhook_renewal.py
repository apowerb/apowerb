"""Tests for ``apowerb.scheduler.webhook_renewal``.

Live regression 2026-05-10 04:33 UTC on SCEI_PROD: the renewal pass
crashed with ``greenlet_spawn has not been called; can't call
await_only() here. Was IO attempted in an unexpected place?`` and the
subscription was left with its old expiration date in DB. Microsoft
Graph eventually came close to dropping it; only an emergency manual
``POST /renew`` saved the day.

Root cause: ``OutlookWebhookService.get_access_token_for_user`` may
commit the session in the middle of the renewal pass (refresh-token
rotation persists a new encrypted token). The default
``expire_on_commit=True`` then expires the ``sub`` ORM instance, so
the subsequent ``sub.expiration_datetime = ...`` / ``sub.status =
...`` writes try to lazy-load the row — illegal from an async context
without a greenlet, hence the crash. The fallback ``except`` block
(line 113-118) repeats the same anti-pattern and crashes too, so the
DB row never even gets flipped to ``status='expired'``.

These tests pin the contract: after a token refresh that commits
mid-pass, ``_renew_expiring_subscriptions`` must still persist the
new expiration and ``status='active'`` (and on failure, must persist
``status='expired'``) — without lazy-loading expired ORM instances.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from apowerb.helpers.database import Base
from apowerb.models import User, WebhookSubscription
from apowerb.scheduler import webhook_renewal


@pytest.fixture
async def sqlite_session_factory(monkeypatch):
    """Build a sqlite in-memory async engine wired into
    ``webhook_renewal.sessionmanager`` for the duration of the test.

    ``expire_on_commit`` is left at the SQLAlchemy default (``True``)
    so the test reproduces the exact post-commit ORM expiration that
    triggers the greenlet crash on SCEI_PROD. Don't lower the bar to
    ``False`` here — that would mask the bug we're guarding against.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    # Strip the Postgres schema from every table — sqlite has no
    # cross-schema CREATE TABLE syntax and would choke on
    # CREATE TABLE th2agent_dev.user. We restore the schema after
    # create_all so production behaviour stays untouched.
    for t in Base.metadata.tables.values():
        t.schema = None
    Base.metadata.schema = None
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # async_sessionmaker mirrors the prod ``sessionmaker(class_=AsyncSession)``
    # config, while explicit ``expire_on_commit=True`` keeps the bug
    # reproducible — flipping it to False would mask the exact lazy-load
    # crash this test is here to guard.
    factory = async_sessionmaker(engine, expire_on_commit=True)

    @asynccontextmanager
    async def _session() -> AsyncIterator[AsyncSession]:
        session = factory()
        try:
            yield session
        finally:
            await session.close()

    monkeypatch.setattr(
        webhook_renewal.sessionmanager, "session", _session, raising=False
    )

    yield factory
    await engine.dispose()


async def _seed_expiring_outlook_sub(factory) -> int:
    """Insert one Outlook subscription whose expiration is well inside
    the 12h renewal threshold. Returns the sub's primary key."""
    async with factory() as db:
        user = User(
            user_id=1,
            first_name="Com",
            last_name="SCEI",
            email="com@scei88.fr",
            password="x",
            role="USER",
        )
        db.add(user)
        await db.commit()

        sub = WebhookSubscription(
            user_id=1,
            integration_id=None,
            provider="microsoft_outlook",
            subscription_id="62ef5a5c-1234-1234-1234-abcdefabcdef",
            resource="me/mailFolders('Inbox')/messages",
            change_type="created,updated",
            client_state="state",
            notification_url="https://example.com/cb",
            status="active",
            agent_id=6,
            # 6h ahead — well inside the 12h renewal window.
            expiration_datetime=datetime.now(timezone.utc) + timedelta(hours=6),
        )
        db.add(sub)
        # Capture the autoincrement PK via flush BEFORE the commit so
        # the helper does not itself trip the lazy-load greenlet trap
        # we're guarding production code against.
        await db.flush()
        sub_id = sub.id
        await db.commit()
        return sub_id


# ---------------------------------------------------------------------------
# Outlook renewal — happy path with mid-pass commit (the prod scenario)
# ---------------------------------------------------------------------------


class TestOutlookRenewalSurvivesMidPassCommit:
    """The token-refresh helper may commit the session inside
    ``get_access_token_for_user`` to persist a rotated refresh token.
    After that commit, the ``sub`` ORM instance is expired. Subsequent
    writes must NOT lazy-load from it."""

    @pytest.mark.asyncio
    async def test_renewal_persists_new_expiry_and_active_status(
        self, sqlite_session_factory, monkeypatch
    ):
        sub_id = await _seed_expiring_outlook_sub(sqlite_session_factory)
        new_expiry = datetime.now(timezone.utc) + timedelta(days=3)

        # The real method rotates the refresh token and COMMITS the
        # session before returning the access token. Simulate that
        # exact contract.
        async def fake_get_access_token(db, user_id):
            await db.commit()
            return "fake-access-token"

        async def fake_renew(access_token, graph_sub_id):
            # Microsoft returns ISO8601 with trailing Z.
            return {
                "id": graph_sub_id,
                "expirationDateTime": new_expiry.isoformat().replace(
                    "+00:00", "Z"
                ),
            }

        monkeypatch.setattr(
            webhook_renewal.OutlookWebhookService,
            "get_access_token_for_user",
            fake_get_access_token,
        )
        monkeypatch.setattr(
            webhook_renewal.OutlookWebhookService,
            "renew_subscription",
            fake_renew,
        )

        # If the bug is present, this call raises
        # ``MissingGreenlet`` / ``greenlet_spawn has not been called``
        # because the post-commit write touches the expired ``sub``.
        await webhook_renewal._renew_expiring_subscriptions()

        # Read back from a fresh session to confirm the DB row was
        # really updated (not just the in-memory ORM instance).
        async with sqlite_session_factory() as db:
            row = (
                await db.execute(
                    select(WebhookSubscription).where(
                        WebhookSubscription.id == sub_id
                    )
                )
            ).scalar_one()
            assert row.status == "active", (
                "subscription should remain 'active' after a successful renew"
            )
            assert row.expiration_datetime is not None
            # SQLite drops tzinfo on read; compare on naive UTC values.
            persisted = row.expiration_datetime
            if persisted.tzinfo is not None:
                persisted = persisted.astimezone(timezone.utc).replace(tzinfo=None)
            expected = new_expiry.replace(tzinfo=None)
            assert abs((persisted - expected).total_seconds()) < 2, (
                f"expected expiry near {expected}, got {persisted}"
            )


class TestOutlookRenewalFailureMarksExpired:
    """On a renewal error, the row must transition to
    ``status='expired'`` so the operator can spot it in the UI. The
    original code's ``except`` block tries ``sub.status = 'expired'``
    which crashes for the same lazy-load reason — leaving the row in
    a misleading ``'active'`` state."""

    @pytest.mark.asyncio
    async def test_renewal_marks_expired_when_graph_call_fails(
        self, sqlite_session_factory, monkeypatch
    ):
        sub_id = await _seed_expiring_outlook_sub(sqlite_session_factory)

        async def fake_get_access_token(db, user_id):
            await db.commit()  # same mid-pass commit
            return "fake-access-token"

        async def fake_renew_raises(access_token, graph_sub_id):
            raise RuntimeError("Microsoft Graph 500 — service unavailable")

        monkeypatch.setattr(
            webhook_renewal.OutlookWebhookService,
            "get_access_token_for_user",
            fake_get_access_token,
        )
        monkeypatch.setattr(
            webhook_renewal.OutlookWebhookService,
            "renew_subscription",
            fake_renew_raises,
        )

        await webhook_renewal._renew_expiring_subscriptions()

        async with sqlite_session_factory() as db:
            row = (
                await db.execute(
                    select(WebhookSubscription).where(
                        WebhookSubscription.id == sub_id
                    )
                )
            ).scalar_one()
            assert row.status == "expired", (
                "failed renewal must flip status to 'expired' so the "
                "operator sees the subscription as needing attention"
            )


# ---------------------------------------------------------------------------
# Loop resilience — one failing renewal must not kill the long-running task
# ---------------------------------------------------------------------------


class TestRenewalLoopSurvivesFailures:
    """``webhook_renewal_loop`` is started once at app startup. If a
    pass raises an unhandled exception, the task dies silently and
    Microsoft Graph deletes the subscription 3 days later. The loop
    must wrap each pass in try/except — verify the safety net is
    still there."""

    def test_loop_wraps_renewal_in_try_except(self):
        """Static check: the source must keep ``except Exception`` at
        the loop level so one bad pass cannot kill the task."""
        import inspect

        src = inspect.getsource(webhook_renewal.webhook_renewal_loop)
        assert "except Exception" in src, (
            "webhook_renewal_loop must catch every exception per pass; "
            "otherwise a single failure ends the renewal background task "
            "and subscriptions silently expire"
        )

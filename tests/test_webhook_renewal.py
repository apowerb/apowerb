"""Tests for ``apowerb.scheduler.webhook_renewal``.

Live regression 2026-05-10 04:33 UTC on a client's production server: the renewal pass
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

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from apowerb.models import User, WebhookSubscription
from apowerb.scheduler import webhook_renewal


@pytest.fixture
async def sqlite_session_factory(sqlite_engine, bind_sessionmanager):
    """Build a sqlite in-memory async engine wired into
    ``webhook_renewal.sessionmanager`` for the duration of the test.

    ``expire_on_commit`` is left at the SQLAlchemy default (``True``)
    so the test reproduces the exact post-commit ORM expiration that
    triggers the greenlet crash on a client's production server. Don't lower the bar to
    ``False`` here — that would mask the bug we're guarding against.
    """
    # async_sessionmaker mirrors the prod ``sessionmaker(class_=AsyncSession)``
    # config, while explicit ``expire_on_commit=True`` keeps the bug
    # reproducible — flipping it to False would mask the exact lazy-load
    # crash this test is here to guard.
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=True)

    bind_sessionmanager(factory)

    yield factory


async def _seed_expiring_outlook_sub(
    factory, expires_in: timedelta = timedelta(hours=6)
) -> int:
    """Insert one Outlook subscription whose expiration is well inside
    the 12h renewal threshold (or already past, with a negative
    *expires_in*). Returns the sub's primary key."""
    async with factory() as db:
        user = User(
            user_id=1,
            first_name="Com",
            last_name="Client",
            email="ops@acme.example",
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
            # Default 6h ahead — well inside the 12h renewal window.
            expiration_datetime=datetime.now(timezone.utc) + expires_in,
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


async def _read_sub(factory, sub_id: int) -> WebhookSubscription:
    async with factory() as db:
        return (
            await db.execute(
                select(WebhookSubscription).where(WebhookSubscription.id == sub_id)
            )
        ).scalar_one()


def _patch_graph(monkeypatch, *, token, renew) -> None:
    monkeypatch.setattr(
        webhook_renewal.OutlookWebhookService, "get_access_token_for_user", token
    )
    monkeypatch.setattr(
        webhook_renewal.OutlookWebhookService, "renew_subscription", renew
    )


async def _token_ok(db, user_id):
    await db.commit()  # same mid-pass commit as the real helper
    return "fake-access-token"


async def _token_401(db, user_id):
    # Live incident 2026-11-02 (client production): the Azure app client
    # secret expired, every token refresh answered 401 AADSTS7000222.
    raise RuntimeError("Failed to refresh Outlook access token (HTTP 401).")


async def _graph_500(access_token, graph_sub_id):
    raise RuntimeError("Microsoft Graph 500 — service unavailable")


class TestOutlookRenewalFailureKeepsRetrying:
    """A failed renewal must not abandon a subscription that Microsoft
    Graph still holds.

    Live incident 2026-11-02 (client production): a token refresh failed
    10h before the Graph expiry; the pass flipped the row to
    ``status='expired'``, the query only selects ``'active'`` rows, so no
    later pass ever retried. The row may only be marked ``'expired'``
    once its expiration date is actually past."""

    @pytest.mark.asyncio
    async def test_failure_before_expiry_keeps_row_active(
        self, sqlite_session_factory, monkeypatch
    ):
        sub_id = await _seed_expiring_outlook_sub(sqlite_session_factory)
        _patch_graph(monkeypatch, token=_token_ok, renew=_graph_500)

        await webhook_renewal._renew_expiring_subscriptions()

        row = await _read_sub(sqlite_session_factory, sub_id)
        assert row.status == "active", (
            "the Graph subscription is still alive: the row must stay "
            "'active' so the next pass retries it"
        )

    @pytest.mark.asyncio
    async def test_next_pass_retries_after_a_token_failure(
        self, sqlite_session_factory, monkeypatch
    ):
        sub_id = await _seed_expiring_outlook_sub(sqlite_session_factory)
        new_expiry = datetime.now(timezone.utc) + timedelta(days=3)
        renew_calls: list[str] = []

        async def fake_renew(access_token, graph_sub_id):
            renew_calls.append(graph_sub_id)
            return {"expirationDateTime": new_expiry.isoformat()}

        _patch_graph(monkeypatch, token=_token_401, renew=fake_renew)
        await webhook_renewal._renew_expiring_subscriptions()

        # Credentials fixed between two passes (secret rotated).
        _patch_graph(monkeypatch, token=_token_ok, renew=fake_renew)
        await webhook_renewal._renew_expiring_subscriptions()

        assert len(renew_calls) == 1, "the second pass must retry the renewal"
        row = await _read_sub(sqlite_session_factory, sub_id)
        assert row.status == "active"
        persisted = row.expiration_datetime
        if persisted.tzinfo is not None:
            persisted = persisted.astimezone(timezone.utc).replace(tzinfo=None)
        expected = new_expiry.replace(tzinfo=None)
        assert abs((persisted - expected).total_seconds()) < 2

    @pytest.mark.asyncio
    async def test_failure_after_expiry_marks_expired(
        self, sqlite_session_factory, monkeypatch
    ):
        """Once the expiry is past, Graph has dropped the subscription:
        flip to ``'expired'`` so the operator sees it in the UI."""
        sub_id = await _seed_expiring_outlook_sub(
            sqlite_session_factory, expires_in=timedelta(hours=-1)
        )
        _patch_graph(monkeypatch, token=_token_ok, renew=_graph_500)

        await webhook_renewal._renew_expiring_subscriptions()

        row = await _read_sub(sqlite_session_factory, sub_id)
        assert row.status == "expired"


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

    def test_no_silent_exception_swallowing_in_renewal(self):
        """Verify webhook_renewal does not swallow errors with silent pass."""
        import inspect

        src = inspect.getsource(webhook_renewal)
        assert "except Exception:\n                    pass" not in src
        assert "except Exception:\n                pass" not in src


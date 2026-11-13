"""A dead mail subscription must be visible from outside, and must alert.

Live incident 2026-11-02 → 2026-11-09 (client production): the Outlook
subscription stopped renewing, then expired, and no mail reached the agent for
seven days. The only alert went through the same Microsoft credential that had
failed, so it never left. Two signals independent of that credential:

* ``GET /health/webhooks`` answers 503 as soon as a subscription is overdue for
  renewal (less than ``STALE_HOURS`` left: one renewal pass at least has
  failed) or a mailbox is left without any active subscription. It is meant
  to be polled by an external monitor and only exposes counts.
* the renewal pass alerts (``notify_etl``) when it flips a subscription to
  ``expired`` — the moment mail stops arriving.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker

from apowerb.helpers import webhook_health
from apowerb.models import User, WebhookSubscription
from apowerb.routers import health as health_router
from apowerb.scheduler import webhook_renewal

NOW = datetime(2026, 11, 2, 16, 0, tzinfo=timezone.utc)


@pytest.fixture
async def factory(sqlite_engine, bind_sessionmanager):
    f = async_sessionmaker(sqlite_engine, expire_on_commit=True)
    bind_sessionmanager(f)
    async with f() as db:
        db.add(User(user_id=1, first_name="Com", last_name="Client",
                    email="ops@acme.example", password="x", role="USER"))
        await db.commit()
    yield f


async def _sub(factory, *, status: str, expires_in: timedelta,
               resource: str = "me/mailFolders('Inbox')/messages") -> int:
    async with factory() as db:
        sub = WebhookSubscription(
            user_id=1, provider="microsoft_outlook", subscription_id=f"g-{status}-{expires_in}",
            resource=resource, change_type="created,updated", client_state="s",
            notification_url="https://example.com/cb", status=status, agent_id=12,
            expiration_datetime=NOW + expires_in,
        )
        db.add(sub)
        await db.flush()
        sub_id = sub.id
        await db.commit()
        return sub_id


@pytest.mark.asyncio
async def test_renewed_subscription_is_healthy(factory):
    await _sub(factory, status="active", expires_in=timedelta(hours=60))
    res = await webhook_health.check_webhook_health(now=NOW)
    assert res["healthy"] is True


@pytest.mark.asyncio
async def test_subscription_overdue_for_renewal_is_degraded(factory):
    # The cron renews under 12 h left, every 6 h: under 6 h left means at
    # least one pass failed — mail still arrives, but not for long.
    await _sub(factory, status="active", expires_in=timedelta(hours=3))
    res = await webhook_health.check_webhook_health(now=NOW)
    assert res["healthy"] is False
    assert res["counts"]["overdue"] == 1


@pytest.mark.asyncio
async def test_mailbox_left_without_active_subscription_is_degraded(factory):
    await _sub(factory, status="expired", expires_in=timedelta(hours=-30))
    res = await webhook_health.check_webhook_health(now=NOW)
    assert res["healthy"] is False
    assert res["counts"]["mailboxes_without_active"] == 1


@pytest.mark.asyncio
async def test_expired_row_kept_next_to_its_replacement_is_healthy(factory):
    # The old row stays (it carries the history); a new active one replaced it.
    await _sub(factory, status="expired", expires_in=timedelta(hours=-30))
    await _sub(factory, status="active", expires_in=timedelta(hours=60))
    res = await webhook_health.check_webhook_health(now=NOW)
    assert res["healthy"] is True


@pytest.mark.asyncio
async def test_no_subscription_at_all_is_healthy(factory):
    res = await webhook_health.check_webhook_health(now=NOW)
    assert res["healthy"] is True


@pytest.mark.asyncio
async def test_endpoint_answers_503_with_counts_only(factory, monkeypatch):
    await _sub(factory, status="expired", expires_in=timedelta(hours=-30))
    with pytest.raises(HTTPException) as exc:
        await health_router.webhooks_health_endpoint()
    assert exc.value.status_code == 503
    detail = exc.value.detail
    assert detail["status"] == "degraded"
    # Unauthenticated probe: no ids, no resource, no e-mail.
    flat = repr(detail)
    assert "g-expired" not in flat and "Inbox" not in flat and "acme" not in flat


@pytest.mark.asyncio
async def test_renewal_pass_alerts_when_it_expires_a_subscription(factory, monkeypatch):
    sub_id = await _sub(factory, status="active", expires_in=timedelta(hours=-1))
    sent = []

    async def _notify(job, error, *, context=None, now=None):
        sent.append((job, error, context))
        return True

    monkeypatch.setattr(webhook_renewal.notify_etl, "notify_job_failure", _notify)

    async def _boom(db, user_id):
        raise RuntimeError("AADSTS7000222 client secret expired")

    monkeypatch.setattr(webhook_renewal.OutlookWebhookService, "get_access_token_for_user", _boom)

    await webhook_renewal._renew_expiring_subscriptions()

    expired = [s for s in sent if s[0] == "webhook-expired"]
    assert len(expired) == 1, sent
    assert f"sub_db_id={sub_id}" in (expired[0][2] or "")

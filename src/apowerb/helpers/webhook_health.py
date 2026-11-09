"""Health of the mail subscriptions, for an external monitor.

Live incident 2026-11-02 -> 2026-11-09 (client production): the Outlook
subscription stopped renewing, expired, and no mail reached the agent for
seven days. The renewal-failure alert went out through the same Microsoft
credential that had failed, so it never left. This check gives a signal that
does not depend on that credential: ``GET /health/webhooks`` (503 when
degraded), meant to be polled from outside.

Degraded when:

* an ``active`` subscription has less than ``STALE_HOURS`` left. The cron
  renews under ``RENEW_THRESHOLD_HOURS`` left, every ``CHECK_INTERVAL_SECONDS``,
  so a healthy subscription never gets that close: at least one renewal pass
  has failed and mail stops at the expiry;
* a mailbox (same user, provider and resource) has subscriptions but none is
  ``active``: mail no longer arrives. An expired row kept next to its active
  replacement (it carries the history) is fine.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from apowerb.helpers.database import sessionmanager
from apowerb.models import WebhookSubscription
from apowerb.scheduler.webhook_renewal import CHECK_INTERVAL_SECONDS, RENEW_THRESHOLD_HOURS

STALE_HOURS = RENEW_THRESHOLD_HOURS - CHECK_INTERVAL_SECONDS / 3600


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


async def check_webhook_health(*, now: datetime | None = None) -> dict:
    """Return ``{"healthy": bool, "counts": {...}}`` — counts only, no ids."""
    now = now or datetime.now(timezone.utc)
    stale_before = now + timedelta(hours=STALE_HOURS)
    async with sessionmanager.session() as db:
        rows = (await db.execute(select(
            WebhookSubscription.user_id, WebhookSubscription.provider,
            WebhookSubscription.resource, WebhookSubscription.status,
            WebhookSubscription.expiration_datetime,
        ))).all()

    mailboxes: dict[tuple, bool] = {}
    active = overdue = 0
    for user_id, provider, resource, status, expiry in rows:
        key = (user_id, provider, resource)
        is_active = status == "active"
        mailboxes[key] = mailboxes.get(key, False) or is_active
        if is_active:
            active += 1
            expiry = _aware(expiry)
            if expiry is not None and expiry < stale_before:
                overdue += 1
    without_active = sum(1 for has_active in mailboxes.values() if not has_active)
    return {
        "healthy": overdue == 0 and without_active == 0,
        "counts": {
            "active": active,
            "overdue": overdue,
            "mailboxes_without_active": without_active,
        },
    }

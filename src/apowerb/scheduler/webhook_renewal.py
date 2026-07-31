"""Background task that auto-renews webhook subscriptions.

Supports both Microsoft Graph (Outlook) and Gmail (Pub/Sub watch)
subscriptions.  Runs as an ``asyncio.Task`` started at application
startup.  Every ``CHECK_INTERVAL_SECONDS`` it queries for active
subscriptions expiring within the next ``RENEW_THRESHOLD_HOURS`` and
renews them via the appropriate provider API.  Subscriptions that
fail to renew are marked as ``"expired"``.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from logging import getLogger

from sqlalchemy import delete, select, update

from apowerb.configs.settings import get_settings
from apowerb.helpers.database import sessionmanager
from apowerb.integrations.gmail_webhook import GmailWebhookService
from apowerb.integrations.outlook_webhook import OutlookWebhookService
from apowerb.models import WebhookSubscription
from apowerb.helpers import notify_etl

logger = getLogger(__name__)

# How often to check for subscriptions that need renewal
CHECK_INTERVAL_SECONDS = 6 * 3600  # every 6 hours

# Renew subscriptions expiring within this window
RENEW_THRESHOLD_HOURS = 12


async def _renew_expiring_subscriptions() -> None:
    """Single renewal pass -- find and renew subscriptions close to expiry."""
    threshold = datetime.now(timezone.utc) + timedelta(hours=RENEW_THRESHOLD_HOURS)
    settings = get_settings()

    async with sessionmanager.session() as db:
        # ── 1. Renew Outlook / Microsoft Graph subscriptions ──────────
        result = await db.execute(
            select(WebhookSubscription).where(
                WebhookSubscription.status == "active",
                WebhookSubscription.provider == "microsoft_outlook",
                WebhookSubscription.expiration_datetime <= threshold,
                WebhookSubscription.subscription_id.is_not(None),
            )
        )
        outlook_subs = result.scalars().all()

        if outlook_subs:
            logger.info(
                "[WEBHOOK CRON] Found %d Outlook subscription(s) to renew.",
                len(outlook_subs),
            )

        for sub in outlook_subs:
            # Snapshot the PK and graph id BEFORE any await that might
            # commit. ``get_access_token_for_user`` rotates the refresh
            # token and commits the session under the hood — that commit
            # expires every ORM instance attached to ``db`` (default
            # ``expire_on_commit=True``). Touching ``sub.<attr>`` after
            # that point triggers a lazy load from an async context, which
            # raises ``greenlet_spawn has not been called`` and kills the
            # whole pass. Live regression 2026-05-10 04:33 UTC SCEI_PROD.
            sub_db_id = sub.id
            sub_graph_id = sub.subscription_id
            sub_user_id = sub.user_id
            try:
                access_token = await OutlookWebhookService.get_access_token_for_user(
                    db, sub_user_id
                )
                renewed = await OutlookWebhookService.renew_subscription(
                    access_token, sub_graph_id
                )

                # Persist the new state via a primary-key UPDATE rather
                # than mutating the (now-expired) ORM instance.
                values: dict = {"status": "active"}
                new_expiry_str = renewed.get("expirationDateTime")
                new_expiry = None
                if new_expiry_str:
                    try:
                        new_expiry = datetime.fromisoformat(
                            new_expiry_str.replace("Z", "+00:00")
                        )
                        values["expiration_datetime"] = new_expiry
                    except (ValueError, TypeError):
                        pass

                await db.execute(
                    update(WebhookSubscription)
                    .where(WebhookSubscription.id == sub_db_id)
                    .values(**values)
                )
                await db.commit()

                logger.info(
                    "[WEBHOOK CRON] Renewed sub_db_id=%s (graph_id=%s), new_expiry=%s",
                    sub_db_id,
                    sub_graph_id,
                    new_expiry,
                )

            except LookupError:
                # 404 — subscription gone from Microsoft Graph; delete locally.
                logger.warning(
                    "[WEBHOOK CRON] sub_db_id=%s (graph_id=%s) not found on "
                    "Microsoft Graph — deleting local record.",
                    sub_db_id,
                    sub_graph_id,
                )
                try:
                    await db.execute(
                        delete(WebhookSubscription).where(
                            WebhookSubscription.id == sub_db_id
                        )
                    )
                    await db.commit()
                except Exception:
                    pass

            except Exception as exc:
                logger.error(
                    "[WEBHOOK CRON] Failed to renew sub_db_id=%s (graph_id=%s): %s",
                    sub_db_id,
                    sub_graph_id,
                    exc,
                )
                await notify_etl.notify_job_failure(
                    "webhook-renewal", f"{type(exc).__name__}: {exc}",
                    context=f"sub_db_id={sub_db_id}",
                )
                # Mark as expired so the operator notices it in the UI.
                # Same UPDATE-by-id pattern — the ORM instance may be
                # expired from the mid-pass token-refresh commit, so
                # ``sub.status = ...`` would re-raise the greenlet error.
                try:
                    await db.execute(
                        update(WebhookSubscription)
                        .where(WebhookSubscription.id == sub_db_id)
                        .values(status="expired")
                    )
                    await db.commit()
                except Exception:
                    pass

        # ── 2. Renew Gmail watches (7-day expiry) ────────────────────
        gmail_result = await db.execute(
            select(WebhookSubscription).where(
                WebhookSubscription.status == "active",
                WebhookSubscription.provider == "google_gmail",
                WebhookSubscription.expiration_datetime <= threshold,
            )
        )
        gmail_subs = gmail_result.scalars().all()

        if gmail_subs:
            logger.info(
                "[WEBHOOK CRON] Found %d Gmail subscription(s) to renew.",
                len(gmail_subs),
            )

        for sub in gmail_subs:
            # Snapshot PK + attrs before any await that may commit. See
            # the Outlook branch above for the full rationale — same bug
            # class applies here (gmail token refresh also rotates &
            # commits).
            sub_db_id = sub.id
            sub_resource = sub.resource
            sub_user_id = sub.user_id
            try:
                access_token = await GmailWebhookService.get_access_token_for_user(
                    db, sub_user_id
                )
                topic_name = (
                    f"projects/{settings.gmail_pubsub_project_id}"
                    f"/topics/{settings.gmail_pubsub_topic}"
                )
                label_ids = [] if sub_resource == "ALL" else ([sub_resource] if sub_resource else ["INBOX"])
                watch_result = await GmailWebhookService.watch_mailbox(
                    access_token, topic_name, label_ids
                )

                values: dict = {"status": "active"}
                new_expiry = None
                exp_ms = watch_result.get("expiration")
                if exp_ms:
                    try:
                        new_expiry = datetime.fromtimestamp(
                            int(exp_ms) / 1000, tz=timezone.utc
                        )
                        values["expiration_datetime"] = new_expiry
                    except (ValueError, TypeError, OSError):
                        pass

                if watch_result.get("historyId"):
                    values["last_history_id"] = str(watch_result["historyId"])

                await db.execute(
                    update(WebhookSubscription)
                    .where(WebhookSubscription.id == sub_db_id)
                    .values(**values)
                )
                await db.commit()

                logger.info(
                    "[WEBHOOK CRON] Renewed Gmail watch sub_db_id=%s, new_expiry=%s",
                    sub_db_id,
                    new_expiry,
                )

            except Exception as exc:
                logger.error(
                    "[WEBHOOK CRON] Failed to renew Gmail watch sub_db_id=%s: %s",
                    sub_db_id,
                    exc,
                )
                try:
                    await db.execute(
                        update(WebhookSubscription)
                        .where(WebhookSubscription.id == sub_db_id)
                        .values(status="expired")
                    )
                    await db.commit()
                except Exception:
                    pass

    # Also mark any past-due subscriptions as expired (both providers)
    async with sessionmanager.session() as db:
        now = datetime.now(timezone.utc)
        await db.execute(
            update(WebhookSubscription)
            .where(
                WebhookSubscription.status == "active",
                WebhookSubscription.expiration_datetime <= now,
            )
            .values(status="expired")
        )
        await db.commit()


async def webhook_renewal_loop() -> None:
    """Long-running loop -- call once at app startup via asyncio.create_task()."""
    logger.info(
        "[WEBHOOK CRON] Renewal loop started (interval=%ds, threshold=%dh).",
        CHECK_INTERVAL_SECONDS,
        RENEW_THRESHOLD_HOURS,
    )

    # Small delay to let the app fully start
    await asyncio.sleep(30)

    while True:
        try:
            await _renew_expiring_subscriptions()
        except Exception as exc:
            logger.error("[WEBHOOK CRON] Unexpected error in renewal loop: %s", exc, exc_info=True)

        await asyncio.sleep(CHECK_INTERVAL_SECONDS)

"""Periodic watch on the notifier owner integration.

Alerts when the owner integration goes unhealthy. CRITICAL: the alert must NOT
go through the shared mailbox (that is exactly what is down). It uses two
channels independent of it:

1. A loud structured ERROR log (always) — catchable by log-based monitoring.
2. A best-effort ``email_sender`` (direct SMTP) message to ``super_admin_email``
   — sends only if SMTP is configured, otherwise it degrades to a log line.

The robust external signal is ``GET /health/notifier`` (503 when down), meant
to be polled by uptime monitoring. This loop is the proactive complement.
"""

from __future__ import annotations

import asyncio
import time

from apowerb.configs.settings import get_settings
from apowerb.configs.th2logger import setup_logging
from apowerb.helpers import email_sender
from apowerb.helpers.notifier_health import check_notifier_owner

logger = setup_logging(__name__)

CHECK_INTERVAL_SECONDS = 3600  # hourly
_RE_ALERT_SECONDS = 6 * 3600  # while still down, re-alert at most every 6h


async def _alert_owner_down(detail: str, owner: str) -> None:
    settings = get_settings()
    logger.error(
        "notifier_watch: OWNER INTEGRATION DOWN — system emails will NOT be "
        "sent via %s. owner=%s detail=%s",
        settings.notification_email,
        owner,
        detail,
    )
    subject = "[th2agent] Notifier HS — intégration owner indisponible"
    body = (
        f"L'intégration Outlook de l'owner du notifier ({owner}) est "
        f"indisponible : {detail}.\n\n"
        f"Conséquence : les e-mails système (vérification d'inscription, reset "
        f"de mot de passe, alertes ETL) ne partent plus via la boîte partagée "
        f"{settings.notification_email}.\n\n"
        f"Action : reconnecter l'intégration Outlook du compte {owner} (page "
        f"Intégrations) en cochant la boîte partagée {settings.notification_email}.\n"
    )
    # Independent channel — direct SMTP, NOT the shared mailbox.
    try:
        await email_sender.send_email(
            to=settings.super_admin_email, subject=subject, body=body
        )
    except Exception as exc:  # never let alerting crash the loop
        logger.error("notifier_watch: SMTP alert dispatch failed: %s", exc)


async def notifier_watch_loop() -> None:
    """Forever loop. Hourly deep health check, edge-triggered alert on failure."""
    if not get_settings().notification_integration_owner.strip():
        logger.info(
            "notifier_watch: no notifier owner configured — system mailer off, "
            "watch not started"
        )
        return
    logger.info("notifier_watch: loop started (interval=%ds)", CHECK_INTERVAL_SECONDS)
    await asyncio.sleep(60)  # let startup settle
    was_healthy = True
    last_alert = 0.0
    while True:
        try:
            res = await check_notifier_owner(deep=True)
            if res["healthy"]:
                if not was_healthy:
                    logger.info(
                        "notifier_watch: owner integration RECOVERED (%s)", res["owner"]
                    )
                was_healthy = True
            else:
                now = time.monotonic()
                if was_healthy or (now - last_alert) >= _RE_ALERT_SECONDS:
                    await _alert_owner_down(res["detail"], res["owner"])
                    last_alert = now
                was_healthy = False
        except Exception as exc:  # the watch must never die silently
            logger.error("notifier_watch: tick raised: %s", exc)
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)

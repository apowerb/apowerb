"""ETL / background-job failure alerts.

Sends a normalized alert e-mail to ``super_admin_email`` when a scheduled job
or pipeline fails, routed through the shared notification mailbox
(:mod:`apowerb.helpers.system_mailer`). A light per-(job, error) throttle
keeps a flapping job from flooding the inbox.

Wire it into a failure path like::

    from apowerb.helpers import notify_etl
    try:
        run_pipeline(...)
    except Exception as exc:
        await notify_etl.notify_job_failure("agents-pipeline", str(exc), context=run_id)
        raise
"""

from __future__ import annotations

import hashlib
import time
from urllib.parse import quote

from apowerb.configs.settings import get_settings
from apowerb.configs.th2logger import setup_logging
from apowerb.helpers import system_mailer

logger = setup_logging(__name__)

# (job, error-signature) -> last-sent epoch seconds. Process-local throttle.
_last_sent: dict[str, float] = {}
_THROTTLE_SECONDS = 900  # 15 min — collapse repeated identical failures


def _signature(job: str, error: str) -> str:
    return hashlib.sha1(f"{job}|{error}".encode()).hexdigest()[:16]


async def notify_job_failure(
    job: str,
    error: str,
    *,
    context: str | None = None,
    now: float | None = None,
) -> bool:
    """Email ``super_admin_email`` about a failed job.

    Deduplicated per (job, error signature) over ``_THROTTLE_SECONDS``.
    Returns ``True`` if an email was dispatched, ``False`` if throttled or the
    send failed. Never raises — alerting must not mask the original failure.
    """
    settings = get_settings()

    # No recipient configured: nothing to alert to. Bail out before building
    # the message, otherwise every failed job logs a mailer error instead of
    # the failure itself.
    if not (
        settings.super_admin_email.strip()
        or (settings.etl_alert_recipients or "").strip()
    ):
        logger.debug("notify_etl: no recipient configured, alert for job=%s dropped", job)
        return False

    sig = _signature(job, error)
    ts = now if now is not None else time.time()

    last = _last_sent.get(sig)
    if last is not None and (ts - last) < _THROTTLE_SECONDS:
        logger.info("notify_etl: throttled duplicate alert for job=%s", job)
        return False
    _last_sent[sig] = ts

    subject = f"[ETL][FAILED] {job}"
    intro = f"Le job <strong>{job}</strong> a échoué." + (
        f" Contexte : {context}." if context else ""
    )
    # Deep-link to the Supervision dashboard, pre-filtered on the failing
    # run/agent (context) or, failing that, the job name.
    ref = context or job
    link_url = (
        f"{settings.app_public_url.rstrip('/')}/supervision?search={quote(ref)}"
    )
    html = system_mailer.render_branded_email(
        heading="⚠️ Échec d’un job ETL",
        intro=intro,
        details=error,
        cta_label="Voir dans la supervision",
        cta_url=link_url,
        note="Alerte automatique (dé-doublonnée 15 min). Vérifie les logs du service concerné.",
    )
    text = f"Job {job} FAILED\n\n{error}" + (
        f"\n\nContexte : {context}" if context else ""
    )

    # Recipients: super admin + any configured extras (e.g. support@), deduped
    # while preserving order.
    raw = [settings.super_admin_email] + [
        r.strip() for r in (settings.etl_alert_recipients or "").split(",")
    ]
    recipients = list(dict.fromkeys(r for r in raw if r))

    try:
        return await system_mailer.send_system_email(
            to=recipients, subject=subject, html=html, text=text,
        )
    except Exception as exc:  # defensive — alerting must never raise
        logger.error("notify_etl: failed to dispatch alert for job=%s: %s", job, exc)
        return False

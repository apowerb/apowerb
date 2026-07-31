"""System mailer — transactional / notification emails sent from the shared
Outlook mailbox (``notification_email``) via Microsoft Graph.

Unlike ``tools_store.portfolio.outlook_mail`` (which sends as the *current
request user*), this module is **headless**: it resolves the integration owner
configured by ``notification_integration_owner`` and uses that account's
refresh token. This lets us send during signup / password reset / ETL cron,
where no user is logged in.

Design notes:
- Never raises to the caller. A failed notification must not break a
  user-facing flow (signup, reset). Returns ``True`` only on a real Graph 202;
  callers that care can branch on the bool.
- On failure it logs an ERROR (so prod failures are loud) and best-effort hands
  off to ``email_sender.send_email`` — which, in dev without SMTP configured,
  logs the body so the verify/reset link stays visible to developers.
"""

from __future__ import annotations

import asyncio

import httpx
from sqlalchemy import select

from apowerb.configs.settings import get_settings
from apowerb.configs.th2logger import setup_logging
from apowerb.helpers import email_sender
from apowerb.helpers.database import sessionmanager
from apowerb.integrations.microsoft import MicrosoftIntegrationService
from apowerb.models import User

logger = setup_logging(__name__)

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# thaink² brand palette (cf. globals.css)
_BRAND_MARINE = "#061551"
_BRAND_AZUR = "#3b82f6"


def render_branded_email(
    *,
    heading: str,
    intro: str,
    cta_label: str | None = None,
    cta_url: str | None = None,
    note: str | None = None,
    details: str | None = None,
) -> str:
    """Render a branded, email-client-safe HTML email (inline styles, table
    layout). Reusable for verification, password reset, ETL alerts.

    ``details`` renders a monospace box (e.g. an error trace for ETL alerts).
    """
    details_html = (
        '<pre style="margin:20px 0 0;padding:14px 16px;background:#0b1020;color:#e5e7eb;'
        'border-radius:8px;font-size:13px;line-height:1.5;white-space:pre-wrap;'
        'word-break:break-word;font-family:SFMono-Regular,Consolas,Menlo,monospace;">'
        f'{details}</pre>'
        if details
        else ""
    )
    button = ""
    if cta_label and cta_url:
        button = (
            '<table role="presentation" cellspacing="0" cellpadding="0" style="margin:26px 0;">'
            f'<tr><td style="border-radius:8px;background:{_BRAND_AZUR};">'
            f'<a href="{cta_url}" style="display:inline-block;padding:13px 30px;font-size:15px;'
            'font-weight:600;color:#ffffff;text-decoration:none;border-radius:8px;">'
            f'{cta_label}</a></td></tr></table>'
        )
    note_html = (
        f'<p style="margin:18px 0 0;color:#6b7280;font-size:13px;line-height:1.5;">{note}</p>'
        if note
        else ""
    )
    return (
        '<!DOCTYPE html><html><body style="margin:0;padding:0;background:#f4f5f7;">'
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        'style="background:#f4f5f7;padding:32px 0;">'
        '<tr><td align="center">'
        '<table role="presentation" width="520" cellspacing="0" cellpadding="0" '
        'style="width:520px;max-width:92%;background:#ffffff;border-radius:14px;overflow:hidden;'
        'box-shadow:0 1px 3px rgba(6,21,81,0.08);'
        'font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">'
        f'<tr><td style="background:{_BRAND_MARINE};padding:22px 32px;">'
        '<span style="color:#ffffff;font-size:18px;font-weight:700;letter-spacing:0.2px;">'
        'thaink²</span></td></tr>'
        '<tr><td style="padding:32px;">'
        f'<h1 style="margin:0 0 12px;color:{_BRAND_MARINE};font-size:20px;font-weight:700;">'
        f'{heading}</h1>'
        f'<p style="margin:0;color:#374151;font-size:15px;line-height:1.6;">{intro}</p>'
        f'{details_html}{button}{note_html}</td></tr>'
        '<tr><td style="padding:18px 32px;background:#f9fafb;border-top:1px solid #eef0f3;">'
        '<p style="margin:0;color:#9ca3af;font-size:12px;line-height:1.5;">'
        'thaink² · E-mail automatique, merci de ne pas y répondre.</p>'
        '</td></tr></table></td></tr></table></body></html>'
    )


async def _get_owner_token() -> str | None:
    """Resolve the notification owner → user_id → a valid Outlook access token.

    Returns ``None`` (and logs) when the owner is unknown in DB or the
    integration is missing / revoked.
    """
    settings = get_settings()
    owner = settings.notification_integration_owner
    async with sessionmanager.session() as db:
        row = (
            await db.execute(select(User.user_id).where(User.email == owner))
        ).first()
        if row is None:
            logger.error(
                "system_mailer: notification owner %r not found in DB", owner
            )
            return None
        try:
            return await MicrosoftIntegrationService.get_valid_access_token(
                db, row[0], "outlook"
            )
        except Exception as exc:  # integration missing / refresh revoked
            logger.error(
                "system_mailer: cannot get Outlook token for owner %r (user_id=%s): %s",
                owner,
                row[0],
                exc,
            )
            return None


def _post_send_mail(*, token: str, shared: str, to, subject: str, html: str) -> bool:
    """POST the message to Graph ``/users/{shared}/sendMail``. Pure HTTP.

    ``to`` may be a single address or a list of addresses.
    """
    recipients = [to] if isinstance(to, str) else list(to)
    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": html},
            "toRecipients": [{"emailAddress": {"address": r}} for r in recipients],
        },
        "saveToSentItems": True,
    }
    try:
        resp = httpx.post(
            f"{_GRAPH_BASE}/users/{shared}/sendMail",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
    except httpx.HTTPError as exc:
        logger.error("system_mailer: HTTP error sending to %s: %s", to, exc)
        return False
    if resp.status_code != 202:
        logger.error(
            "system_mailer: sendMail to %s failed (HTTP %s): %s",
            to,
            resp.status_code,
            resp.text[:300],
        )
        return False
    return True


async def send_system_email(
    *, to, subject: str, html: str, text: str | None = None
) -> bool:
    """Send an email from the shared notification mailbox.

    ``to`` may be a single address or a list of addresses. Returns ``True`` on a
    real Graph 202. On any failure, logs an ERROR and falls back to
    ``email_sender.send_email`` (dev visibility) — still returns ``False`` so
    callers know the primary transport failed.
    """
    settings = get_settings()
    shared = settings.notification_email
    recipients = [to] if isinstance(to, str) else [r for r in to if r]

    token = await _get_owner_token()
    if token:
        # httpx.post is sync — offload to a thread so a slow Graph call can't
        # block the FastAPI event loop (signup/reset run in request handlers).
        sent = await asyncio.to_thread(
            _post_send_mail,
            token=token,
            shared=shared,
            to=recipients,
            subject=subject,
            html=html,
        )
        if sent:
            logger.info("system_mailer: email sent to %s (subject=%r)", recipients, subject)
            return True

    # Fallback — loud failure already logged above; keep the link visible in dev.
    logger.error(
        "system_mailer: falling back to email_sender for %s (subject=%r)",
        recipients,
        subject,
    )
    await email_sender.send_email(
        to=", ".join(recipients), subject=subject, body=text or html
    )
    return False

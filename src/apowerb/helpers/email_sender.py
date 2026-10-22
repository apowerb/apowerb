"""Minimalist SMTP email helper (B18).

Configuration is pulled from the environment (SMTP_HOST / SMTP_PORT /
SMTP_USER / SMTP_PASSWORD / SMTP_FROM). When any of those is missing — typical
in the dev sandbox — the helper falls back to a loguru warning so callers can
keep exercising the happy path without a real SMTP relay.
"""

from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage
from typing import Optional, Sequence

from apowerb.configs.th2logger import setup_logging

logger = setup_logging(__name__)

# (nom, type MIME, octets) d'une pièce jointe.
Attachment = tuple[str, str, bytes]


def _env(name: str) -> Optional[str]:
    v = os.environ.get(name)
    if v is None or not v.strip():
        return None
    return v.strip()


def _smtp_configured() -> bool:
    return all(
        _env(k) is not None
        for k in ("SMTP_HOST", "SMTP_PORT", "SMTP_FROM")
    )


def _send_smtp(
    *, to: str, subject: str, body: str, attachments: Sequence[Attachment] = ()
) -> None:
    host = _env("SMTP_HOST")
    port = int(_env("SMTP_PORT") or "587")
    user = _env("SMTP_USER")
    password = _env("SMTP_PASSWORD")
    sender = _env("SMTP_FROM")

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    for name, content_type, data in attachments:
        maintype, _, subtype = content_type.partition("/")
        msg.add_attachment(
            data,
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=name,
        )

    with smtplib.SMTP(host, port, timeout=10) as client:
        client.ehlo()
        try:
            client.starttls()
            client.ehlo()
        except smtplib.SMTPNotSupportedError:
            # Plain SMTP (local relay or tests) — STARTTLS may be absent.
            pass
        if user and password:
            client.login(user, password)
        client.send_message(msg)


async def send_email(
    *,
    to: str,
    subject: str,
    body: str,
    attachments: Optional[Sequence[Attachment]] = None,
) -> None:
    """Send an email. Falls back to a log-only pretend-send in dev.

    Attachments are logged by name only, never their bytes.
    """
    attachments = list(attachments or ())
    if not _smtp_configured():
        logger.warning(
            "email pretend-sent to %s: subject=%s body=%s attachments=%s",
            to,
            subject,
            body,
            [name for name, _, _ in attachments],
        )
        return

    try:
        # ``smtplib`` is blocking; run it off the event loop.
        import asyncio

        await asyncio.to_thread(
            _send_smtp,
            to=to,
            subject=subject,
            body=body,
            attachments=attachments,
        )
    except Exception as exc:
        logger.error(
            "email send failed to %s: %s; falling back to pretend-send",
            to,
            exc,
        )
        logger.warning(
            "email pretend-sent to %s: subject=%s body=%s",
            to,
            subject,
            body,
        )

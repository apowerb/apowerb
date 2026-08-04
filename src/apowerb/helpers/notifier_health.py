"""Health of the system-mailer owner integration.

The notifier (:mod:`apowerb.helpers.system_mailer`) sends from a shared mailbox
using ONE owner integration (``notification_integration_owner``). If that
integration disappears (deleted) or its refresh token is revoked, every system
email — signup verification, password reset, ETL alerts — silently fails.

This module detects that so it can be surfaced via ``GET /health/notifier`` and
the :mod:`apowerb.scheduler.notifier_watch` loop. It NEVER raises.
"""

from __future__ import annotations

from sqlalchemy import select

from apowerb.configs.settings import get_settings
from apowerb.configs.th2logger import setup_logging
from apowerb.helpers.database import sessionmanager
from apowerb.integrations.microsoft import MicrosoftIntegrationService
from apowerb.models import Integration, User

logger = setup_logging(__name__)

_OUTLOOK_PROVIDER = "microsoft_outlook"


async def check_notifier_owner(*, deep: bool = False) -> dict:
    """Return ``{"healthy": bool, "owner": str, "detail": str}``.

    Cheap mode (default): the owner user exists AND has a ``microsoft_outlook``
    integration carrying a refresh token. Suitable for frequent health polls —
    no network call.

    Deep mode: additionally attempts a token refresh to catch a *revoked*
    refresh token (network call to Microsoft). Used by the periodic watch loop.
    """
    settings = get_settings()
    owner = settings.notification_integration_owner
    # No owner configured: the system mailer is off by choice, not broken.
    # Reporting it unhealthy would raise a 503 and alert hourly about a feature
    # the operator never enabled.
    if not owner.strip():
        return {
            "healthy": True,
            "configured": False,
            "owner": "",
            "detail": "system mailer not configured (NOTIFICATION_INTEGRATION_OWNER unset)",
        }
    try:
        async with sessionmanager.session() as db:
            row = (
                await db.execute(select(User.user_id).where(User.email == owner))
            ).first()
            if row is None:
                return {"healthy": False, "owner": owner, "detail": "owner user not found in DB"}
            uid = row[0]
            ig = (
                await db.execute(
                    select(Integration).where(
                        Integration.user_id == uid,
                        Integration.provider == _OUTLOOK_PROVIDER,
                    )
                )
            ).scalar_one_or_none()
            if ig is None:
                return {"healthy": False, "owner": owner, "detail": "no microsoft_outlook integration"}
            if not ig.refresh_token:
                return {"healthy": False, "owner": owner, "detail": "integration has no refresh_token"}
            if deep:
                token = await MicrosoftIntegrationService.get_valid_access_token(
                    db, uid, "outlook"
                )
                if not token:
                    return {"healthy": False, "owner": owner, "detail": "token refresh failed (revoked?)"}
            return {"healthy": True, "owner": owner, "detail": "ok"}
    except Exception as exc:  # never raise — supervision must be robust
        return {"healthy": False, "owner": owner, "detail": f"{type(exc).__name__}: {exc}"}

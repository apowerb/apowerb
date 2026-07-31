"""Audit log helper (B19).

``audit(event, user_id=..., **details)`` emits a single structured
INFO record on the dedicated ``apowerb.audit`` logger, marked with
``audit=True`` so log pipelines can route audit events separately
from regular application logs (e.g. to a tamper-evident sink).

Sensible events to wire up (minimal list — extend as you go):

- ``auth.login`` / ``auth.logout`` / ``auth.mfa_enabled``
- ``auth.password_reset_requested`` / ``auth.password_reset_completed``
- ``agent.delete`` / ``agent.publish`` / ``agent.clone``
- ``integration.revoke``
- ``billing.checkout`` / ``billing.refund``

Keep ``event`` values short, dotted, and stable — they become
grouping keys in downstream pipelines.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

AUDIT_LOGGER_NAME = "apowerb.audit"

_logger = logging.getLogger(AUDIT_LOGGER_NAME)


def audit(
    event: str,
    *,
    user_id: Optional[Any] = None,
    level: int = logging.INFO,
    **details: Any,
) -> None:
    """Emit a structured audit record.

    ``user_id`` may be ``None`` for unauthenticated actions (e.g.
    public webhook reception) — in that case it's still logged as
    ``user_id=null`` to keep the schema stable.
    """
    extra: dict[str, Any] = {
        "audit": True,
        "event": event,
        "user_id": user_id,
    }
    # Don't clobber the reserved keys above.
    for key, value in details.items():
        if key in {"audit", "event", "user_id"}:
            continue
        extra[key] = value
    _logger.log(level, event, extra=extra)


__all__ = ["audit", "AUDIT_LOGGER_NAME"]

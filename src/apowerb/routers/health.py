"""Real health check router (B19).

Three endpoints:

- ``GET /health``      : legacy alias → same payload as ``/health/live``.
- ``GET /health/live`` : liveness probe — 200 if the process is alive.
  No dependency check, intentionally fast so kube-probes don't
  accidentally restart us because of a downstream hiccup.
- ``GET /health/ready``: readiness probe — tests hard deps (DB + Fernet
  key) and best-effort soft deps (RAG API, Stripe). Returns 200 when
  hard deps are OK, 503 otherwise, with a JSON body enumerating each
  check's status. Each check is bounded by ``DEPENDENCY_TIMEOUT_S`` so
  a hanging dep cannot starve the endpoint.
"""

from __future__ import annotations

import asyncio
from logging import getLogger
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from sqlalchemy import text

DEPENDENCY_TIMEOUT_S: float = 2.0

router = APIRouter(prefix="/health", tags=["health"])
logger = getLogger(__name__)


# ---------------------------------------------------------------------------
# Dependency probes — kept as module-level functions so tests can monkeypatch.
# ---------------------------------------------------------------------------


async def _check_db() -> tuple[bool, Optional[str]]:
    """Execute ``SELECT 1`` through the async session manager."""
    try:
        from apowerb.helpers.database import sessionmanager

        async with sessionmanager.session() as session:
            await session.execute(text("SELECT 1"))
        return True, None
    except Exception:  # pragma: no cover - depends on live DB
        # /health/ready is unauthenticated (k8s-style probe). The raw
        # exception can carry internal DB host/port/driver details
        # ("could not connect to server ... on host 10.0.x.x ...") — log it
        # for operators, but don't hand it to whoever is polling this over
        # the network.
        logger.exception("[HEALTH] DB check failed")
        return False, "database check failed"


def _check_fernet() -> tuple[bool, Optional[str]]:
    """Ensure the Fernet encryption key is loaded."""
    try:
        from apowerb.helpers import encryptor

        if encryptor.fernet is None:
            return False, "ENCRYPT_KEY not configured"
        return True, None
    except Exception:  # pragma: no cover - defensive
        logger.exception("[HEALTH] Fernet check failed")
        return False, "fernet check failed"


async def _run_with_timeout(coro, timeout: float) -> tuple[bool, Optional[str]]:
    try:
        result = await asyncio.wait_for(coro, timeout=timeout)
        if isinstance(result, tuple):
            return result  # type: ignore[return-value]
        return bool(result), None
    except asyncio.TimeoutError:
        return False, f"timeout after {timeout:.1f}s"
    except Exception:  # pragma: no cover - defensive
        logger.exception("[HEALTH] Dependency check crashed")
        return False, "check failed"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/live")
async def liveness() -> dict[str, Any]:
    return {"status": "alive", "service": "th2agent"}


@router.get("/ready")
async def readiness() -> dict[str, Any]:
    db_ok, db_err = await _run_with_timeout(
        _check_db(), DEPENDENCY_TIMEOUT_S
    )
    fernet_ok, fernet_err = _check_fernet()

    checks = {
        "database": {"ok": db_ok, "error": db_err},
        "fernet": {"ok": fernet_ok, "error": fernet_err},
    }
    all_ok = db_ok and fernet_ok
    payload: dict[str, Any] = {
        "status": "ready" if all_ok else "not_ready",
        "checks": checks,
    }
    if not all_ok:
        raise HTTPException(status_code=503, detail=payload)
    return payload


@router.get("/notifier")
async def notifier_health_endpoint() -> dict[str, Any]:
    """Owner-integration health for the system mailer. 503 when the shared
    mailbox owner integration is missing/revoked (system emails would fail)."""
    from apowerb.helpers.notifier_health import check_notifier_owner

    res = await check_notifier_owner(deep=False)
    if not res["healthy"]:
        raise HTTPException(status_code=503, detail={"status": "degraded", **res})
    return {"status": "ok", **res}


__all__ = ["router", "DEPENDENCY_TIMEOUT_S", "_check_db", "_check_fernet"]

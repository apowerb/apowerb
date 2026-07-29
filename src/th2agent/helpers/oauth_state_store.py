"""DB-backed OAuth state store for CSRF protection (C4).

Rationale
---------
OAuth ``state`` tokens MUST be persisted server-side so we can validate them
at the callback stage. An in-memory dict is unsafe in a multi-worker setup
(state issued on worker A can't be consumed by worker B) — this store uses
the shared Postgres table ``oauth_states`` instead.

Each row binds the state token to a specific user and provider, enforces a
TTL (default 10 minutes) and is single-use (``consume`` deletes it).

Usage
-----
>>> state = await oauth_state_store.create(
...     db=db, user_id=1, provider="github"
... )
>>> # …user completes the OAuth redirect…
>>> await oauth_state_store.consume(
...     db=db, state=state, user_id=1, provider="github"
... )
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from th2agent.models import OAuthState


DEFAULT_TTL_SECONDS = 600  # 10 minutes


async def create(
    *,
    db: AsyncSession,
    user_id: int,
    provider: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> str:
    """Generate a cryptographically random state, persist it, return it."""
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)

    db.add(
        OAuthState(
            state=token,
            user_id=user_id,
            provider=provider,
            expires_at=expires_at,
        )
    )
    await db.commit()

    # Best-effort cleanup of expired rows (cheap, happens once per connect).
    try:
        await _cleanup_expired(db)
    except Exception:
        # Never let cleanup failures block a legitimate OAuth connect.
        pass

    return token


async def consume(
    *,
    db: AsyncSession,
    state: str,
    user_id: int,
    provider: str,
) -> None:
    """Validate and delete a state token (single-use).

    Raises
    ------
    HTTPException 400
        If the state is missing, unknown, or expired.
    HTTPException 403
        If the state exists but does not belong to the authenticated user or
        was issued for a different provider.
    """
    if not state or not isinstance(state, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing OAuth state parameter.",
        )

    result = await db.execute(
        select(OAuthState).where(OAuthState.state == state)
    )
    row: OAuthState | None = result.scalar_one_or_none()

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or unknown OAuth state.",
        )

    # Always delete the row, whether validation succeeds or not (one-shot).
    await db.delete(row)
    await db.commit()

    # expires_at is stored as timezone-aware but sqlite/postgres drivers may
    # return naive datetimes; normalise to UTC for comparison safety.
    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if datetime.now(timezone.utc) >= expires_at:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="OAuth state has expired. Please restart the connection flow.",
        )

    if (
        not secrets.compare_digest(str(row.provider), str(provider))
        or row.user_id != user_id
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="OAuth state does not match the authenticated user.",
        )


async def _cleanup_expired(db: AsyncSession) -> int:
    """Delete expired states. Returns the number of rows removed."""
    now = datetime.now(timezone.utc)
    result = await db.execute(
        delete(OAuthState).where(OAuthState.expires_at < now)
    )
    await db.commit()
    return int(getattr(result, "rowcount", 0) or 0)

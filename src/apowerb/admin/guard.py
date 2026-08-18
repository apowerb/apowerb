"""Admin-only access for every route in this extension.

The check goes through the core's own `is_admin`, which normalises the
casing: `role` arrives from `UserRole.value` as "ADMIN", and comparing
against "admin" silently never matched (fixed in apowerb#70). Re-deriving
that test here would be one more place to get it wrong.

It is imported from `helpers/ownership`, its home since apowerb 0.1.35.
It used to come from `evaluation/run_service`, which re-exported it — and
that package left the core in 0.2.0. Since the loader is fail-fast, the
stale import would not have degraded this panel, it would have stopped the
service from starting.
"""

from __future__ import annotations

from apowerb.auth.dependencies import get_current_user
from apowerb.configs.settings import get_settings
from apowerb.helpers.database import get_db
from apowerb.helpers.ownership import is_admin
from apowerb.users import schemas as user_schemas
from fastapi import Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def require_admin(
    current_user: user_schemas.User = Depends(get_current_user),
) -> user_schemas.User:
    """403 for anyone else — not 404: hiding the route's existence from an
    authenticated user buys nothing and makes support harder.
    """
    if not is_admin(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator role required.",
        )
    return current_user


async def is_superadmin(db, user) -> bool:
    """May this administrator cross organisation boundaries?

    While no superadmin has been named, every admin is one. Without that,
    an install could never create its first organisation: the table that
    grants the right can only be written by someone who already has it.
    """
    if not is_admin(user):
        return False

    schema = get_settings().db_schema
    named = (await db.execute(text(
        f"SELECT count(*) FROM {schema}.admin_superadmin"
    ))).scalar() or 0
    if named == 0:
        return True

    row = (await db.execute(text(
        f"SELECT 1 FROM {schema}.admin_superadmin s "
        f'JOIN {schema}."user" u ON u.user_id = s.user_id '
        "WHERE lower(u.email) = lower(:email)"
    ), {"email": user.email})).first()
    return row is not None


async def administered_user_ids(db, user) -> set[int] | None:
    """The users this administrator may see and act on.

    `None` means unrestricted — a superadmin. Otherwise the exact set of
    user_ids in their organisation, which callers must apply when building
    the query, never after fetching rows.

    An org admin belonging to no organisation administers only themselves:
    a scope that resolves to "everyone" on missing data is how a boundary
    silently stops being one.
    """
    if await is_superadmin(db, user):
        return None

    schema = get_settings().db_schema
    org_id = (await db.execute(text(
        f"SELECT m.org_id FROM {schema}.admin_org_member m "
        f'JOIN {schema}."user" u ON u.user_id = m.user_id '
        "WHERE lower(u.email) = lower(:email)"
    ), {"email": user.email})).scalar()

    own = (await db.execute(text(
        f'SELECT user_id FROM {schema}."user" WHERE lower(email) = lower(:email)'
    ), {"email": user.email})).scalar()

    if org_id is None:
        return {own} if own is not None else set()

    members = (await db.execute(text(
        f"SELECT user_id FROM {schema}.admin_org_member WHERE org_id = :org"
    ), {"org": org_id})).scalars().all()
    return set(members) | ({own} if own is not None else set())


async def require_superadmin(
    db: AsyncSession = Depends(get_db),
    current_user: user_schemas.User = Depends(get_current_user),
) -> user_schemas.User:
    """Organisations themselves are a superadmin concern: an org admin who
    could create or delete organisations could grant themselves another one.
    """
    if not await is_superadmin(db, current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Superadministrator role required.",
        )
    return current_user

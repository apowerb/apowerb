"""The control panel's API: users, groups, permissions.

Scope is what was asked for — create users, create groups, manage
permissions. Deleting a user is deliberately absent: it is destructive,
irreversible, and nobody asked for it. Groups can be deleted, because a
group holds no data of its own beyond its memberships.
"""

from __future__ import annotations

from apowerb.configs.settings import get_settings
from apowerb.helpers.database import get_db
from apowerb.helpers.security import get_password_hash
from apowerb.models import User, UserRole
from apowerb.users import schemas as user_schemas
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from apowerb.admin.guard import (
    administered_user_ids,
    is_superadmin,
    require_admin,
)
from apowerb.admin.permissions import PERMISSIONS, unknown_permissions

router = APIRouter(prefix="/admin", tags=["admin"])
settings = get_settings()


def _schema() -> str:
    return settings.db_schema


# --------------------------------------------------------------------------
# Payloads
# --------------------------------------------------------------------------


class UserOut(BaseModel):
    """A user as the control panel needs to see them.

    The counters are what turn a list into something administrable: they say
    who builds, who consumes, and who signed up and never came back. All of
    it already exists — it was simply never asked for.
    """

    user_id: int
    email: str
    first_name: str
    last_name: str
    role: str
    # role=ADMIN *and* listed in admin_superadmin. Kept separate from
    # `role` because the core's enum has no third value — altering it
    # would touch six schemas, production included.
    superadmin: bool = False
    groups: list[str] = []
    # A user belongs to at most one organisation (primary key on user_id),
    # so this is a name, not a list. None means unattached.
    organization: str | None = None
    organization_id: int | None = None
    plan: str | None = None
    created_at: datetime | None = None
    agents: int = 0
    llm_calls: int = 0
    tokens: int = 0
    email_verified: bool = True
    mfa_enabled: bool = False
    # An administrator demanded a second factor. Distinct from
    # `mfa_enabled`: the interesting state is required-and-not-yet-set-up.
    mfa_required: bool = False
    onboarding_completed: bool = True
    # "password" when no identity provider is linked. An account that only
    # ever signed in with one is a real person; the rest are often fixtures.
    sign_in: str = "password"


class NewUser(BaseModel):
    email: EmailStr
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    # Set by the administrator. Hashed with the core's own hasher before it
    # touches the database, and never echoed back in any response.
    password: str = Field(min_length=8, max_length=256)
    role: str = UserRole.USER.value


class RoleChange(BaseModel):
    role: str


class GroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)


class GroupOut(BaseModel):
    group_id: int
    name: str
    description: str | None = None
    members: list[int] = []
    permissions: list[str] = []


class PermissionsIn(BaseModel):
    permissions: list[str]


class MemberIn(BaseModel):
    user_id: int


def _valid_role(role: str) -> str:
    """Accept either casing, store the canonical one. The enum is upper case
    and a lower-case value would be rejected by Postgres, not silently kept.
    """
    try:
        return UserRole[role.strip().upper()].value
    except KeyError:
        allowed = ", ".join(r.value for r in UserRole)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown role {role!r}. Allowed: {allowed}.",
        ) from None


async def _assert_may_act_on(db: AsyncSession, actor, user_id: int) -> None:
    """Refuse a target outside this administrator's organisation.

    Reading was scoped; acting was not. Every route below takes its target
    from the path, so knowing an id was enough to reach anyone — the scope
    held only for someone navigating through the screen.

    404 rather than 403: confirming that an id exists outside your scope is
    already telling you something about an organisation you administer none of.
    """
    allowed = await administered_user_ids(db, actor)
    if allowed is None:          # superadmin
        return
    if user_id not in allowed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No such user."
        )


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------


class MetricPoint(BaseModel):
    day: str
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    sessions: int = 0


class MetricSlice(BaseModel):
    label: str
    tokens: int = 0
    calls: int = 0


class MetricTotals(BaseModel):
    users: int = 0
    # Distinct owners with at least one LLM call inside the window. "Active"
    # has to mean something measurable; an account that exists is not a use.
    users_active: int = 0
    never_used: int = 0
    agents: int = 0
    sessions: int = 0
    llm_calls: int = 0
    tokens: int = 0
    # Usage charged to the platform's own credit rather than a customer key.
    tokens_billed_to_platform: int = 0


class MetricsOut(BaseModel):
    window_days: int
    since: datetime
    scope: str
    totals: MetricTotals
    daily: list[MetricPoint] = []
    top_users: list[MetricSlice] = []
    top_agents: list[MetricSlice] = []
    by_model: list[MetricSlice] = []


@router.get("/metrics", response_model=MetricsOut)
async def platform_metrics(
    days: int = 30,
    db: AsyncSession = Depends(get_db),
    _: user_schemas.User = Depends(require_admin),
):
    """Usage of the platform over a window, scoped to what this
    administrator administers.
    """
    days = max(1, min(days, 365))
    since = datetime.now(timezone.utc) - timedelta(days=days)
    schema = _schema()

    allowed = await administered_user_ids(db, _)
    if allowed is None:
        emails: list[str] | None = None
        scope = "platform"
    else:
        emails = list((await db.execute(text(
            f'SELECT email FROM {schema}."user" WHERE user_id = ANY(:ids)'
        ), {"ids": list(allowed)})).scalars().all())
        scope = "organization"
        # An administrator with nobody to administer has nothing to report,
        # and `= ANY('{}')` would otherwise read as "no filter" to a reader.
        if not emails:
            return MetricsOut(
                window_days=days, since=since, scope=scope, totals=MetricTotals(),
            )

    # Bound once, reused by every query below, so a scoped dashboard cannot
    # accidentally answer one of its panels platform-wide.
    owner_clause = "" if emails is None else " AND owner_id = ANY(:emails)"
    params: dict[str, object] = {"since": since}
    if emails is not None:
        params["emails"] = emails

    # The two tables disagree on timestamps and asyncpg does not forgive it:
    # `llm_usage.created_at` is `timestamp WITH time zone`, while ADK's
    # `sessions.create_time` is `WITHOUT`. Binding one aware value to both
    # fails the second outright. Same instant, tzinfo dropped — which is
    # exactly what a naive UTC column stores.
    session_params: dict[str, object] = dict(params)
    session_params["since"] = since.replace(tzinfo=None)

    totals_row = (await db.execute(text(
        f"SELECT count(*), coalesce(sum(total_tokens),0), "
        f"       count(DISTINCT owner_id), "
        f"       coalesce(sum(CASE WHEN billed_to_thaink2 THEN total_tokens ELSE 0 END),0) "
        f"FROM {schema}.llm_usage WHERE created_at >= :since{owner_clause}"
    ), params)).first()

    users_total = (await db.execute(text(
        f'SELECT count(*) FROM {schema}."user"'
        + ("" if emails is None else " WHERE email = ANY(:emails)")
    ), {} if emails is None else {"emails": emails})).scalar() or 0

    agents_total = (await db.execute(text(
        f"SELECT count(*) FROM {schema}.th2agents_store"
        + ("" if emails is None else " WHERE owner_id = ANY(:emails)")
    ), {} if emails is None else {"emails": emails})).scalar() or 0

    # ADK sessions key their owner as `user_id`, which holds the email.
    session_clause = "" if emails is None else " AND user_id = ANY(:emails)"
    sessions_total = (await db.execute(text(
        f"SELECT count(*) FROM {schema}.sessions "
        f"WHERE create_time >= :since{session_clause}"
    ), session_params)).scalar() or 0

    never_used = (await db.execute(text(
        f'SELECT count(*) FROM {schema}."user" u '
        f"WHERE NOT EXISTS (SELECT 1 FROM {schema}.llm_usage l WHERE l.owner_id = u.email)"
        + ("" if emails is None else " AND u.email = ANY(:emails)")
    ), {} if emails is None else {"emails": emails})).scalar() or 0

    # One row per day, tokens split so the chart can stack them.
    usage_by_day = {
        str(day): (int(inp or 0), int(out or 0), int(calls or 0))
        for day, inp, out, calls in (await db.execute(text(
            f"SELECT date(created_at) AS d, sum(input_tokens), sum(output_tokens), count(*) "
            f"FROM {schema}.llm_usage WHERE created_at >= :since{owner_clause} "
            "GROUP BY d ORDER BY d"
        ), params)).all()
    }
    sessions_by_day = {
        str(day): int(n or 0)
        for day, n in (await db.execute(text(
            f"SELECT date(create_time) AS d, count(*) FROM {schema}.sessions "
            f"WHERE create_time >= :since{session_clause} GROUP BY d ORDER BY d"
        ), session_params)).all()
    }

    # Every day in the window, including the empty ones: a line drawn only
    # through the days that have data hides how quiet the others were.
    start = since.date()
    daily = []
    for offset in range(days + 1):
        day = str(start + timedelta(days=offset))
        inp, out, calls = usage_by_day.get(day, (0, 0, 0))
        daily.append(MetricPoint(
            day=day, input_tokens=inp, output_tokens=out, calls=calls,
            sessions=sessions_by_day.get(day, 0),
        ))

    async def top(column: str) -> list[MetricSlice]:
        rows = (await db.execute(text(
            f"SELECT {column}, sum(total_tokens), count(*) FROM {schema}.llm_usage "
            f"WHERE created_at >= :since AND {column} IS NOT NULL{owner_clause} "
            f"GROUP BY {column} ORDER BY sum(total_tokens) DESC LIMIT 8"
        ), params)).all()
        return [
            MetricSlice(label=str(label), tokens=int(tokens or 0), calls=int(calls or 0))
            for label, tokens, calls in rows
        ]

    return MetricsOut(
        window_days=days,
        since=since,
        scope=scope,
        totals=MetricTotals(
            users=users_total,
            users_active=int(totals_row[2] or 0) if totals_row else 0,
            never_used=never_used,
            agents=agents_total,
            sessions=sessions_total,
            llm_calls=int(totals_row[0] or 0) if totals_row else 0,
            tokens=int(totals_row[1] or 0) if totals_row else 0,
            tokens_billed_to_platform=int(totals_row[3] or 0) if totals_row else 0,
        ),
        daily=daily,
        top_users=await top("owner_id"),
        top_agents=await top("agent_name"),
        by_model=await top("model"),
    )

# --------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------


@router.get("/users", response_model=list[UserOut])
async def list_users(
    db: AsyncSession = Depends(get_db),
    _: user_schemas.User = Depends(require_admin),
):
    """Every user for a superadmin; only their organisation's for an org admin."""
    # None means unrestricted (superadmin); otherwise the exact set of users
    # in this admin's organisation. Applied to the query, never to the rows
    # afterwards — filtering after the fetch is how a boundary leaks.
    allowed = await administered_user_ids(db, _)
    query = select(
        User.user_id, User.email, User.first_name, User.last_name, User.role,
        User.plan, User.created_at, User.email_verified, User.mfa_enabled,
        User.mfa_required,
        User.onboarding_completed, User.google_id, User.microsoft_id,
        User.github_id, User.linkedin_id,
    )
    if allowed is not None:
        query = query.where(User.user_id.in_(allowed))
    rows = (await db.execute(query.order_by(User.email))).all()

    # One query for every membership rather than one per user: the N+1 shape
    # has already been paid for once on this codebase.
    supers = {
        uid
        for (uid,) in (await db.execute(text(
            f"SELECT user_id FROM {_schema()}.admin_superadmin"
        ))).all()
    }

    orgs_by_user = {
        user_id: (org_id, name)
        for user_id, org_id, name in (await db.execute(text(
            f"SELECT m.user_id, o.org_id, o.name FROM {_schema()}.admin_org_member m "
            f"JOIN {_schema()}.admin_organization o ON o.org_id = m.org_id"
        ))).all()
    }

    memberships: dict[int, list[str]] = {}
    for user_id, name in (await db.execute(text(
        f"SELECT m.user_id, g.name FROM {_schema()}.admin_group_member m "
        f"JOIN {_schema()}.admin_group g ON g.group_id = m.group_id"
    ))).all():
        memberships.setdefault(user_id, []).append(name)

    # Both tables key ownership by email, so both aggregate in one pass.
    agents_by_email = {
        email: n
        for email, n in (await db.execute(text(
            f"SELECT owner_id, count(*) FROM {_schema()}.th2agents_store "
            "WHERE owner_id IS NOT NULL GROUP BY owner_id"
        ))).all()
    }
    usage_by_email = {
        email: (calls, int(tokens or 0))
        for email, calls, tokens in (await db.execute(text(
            f"SELECT owner_id, count(*), sum(total_tokens) FROM {_schema()}.llm_usage "
            "WHERE owner_id IS NOT NULL GROUP BY owner_id"
        ))).all()
    }

    def sign_in_of(row) -> str:
        for provider, value in (
            ("google", row.google_id), ("microsoft", row.microsoft_id),
            ("github", row.github_id), ("linkedin", row.linkedin_id),
        ):
            if value:
                return provider
        return "password"

    out = []
    for r in rows:
        calls, tokens = usage_by_email.get(r.email, (0, 0))
        out.append(UserOut(
            user_id=r.user_id, email=r.email, first_name=r.first_name,
            last_name=r.last_name,
            role=r.role.value if hasattr(r.role, "value") else str(r.role),
            superadmin=r.user_id in supers,
            groups=sorted(memberships.get(r.user_id, [])),
            organization=(orgs_by_user.get(r.user_id) or (None, None))[1],
            organization_id=(orgs_by_user.get(r.user_id) or (None, None))[0],
            plan=r.plan,
            created_at=r.created_at,
            agents=agents_by_email.get(r.email, 0),
            llm_calls=calls,
            tokens=tokens,
            email_verified=bool(r.email_verified),
            mfa_enabled=bool(r.mfa_enabled),
            mfa_required=bool(r.mfa_required),
            onboarding_completed=bool(r.onboarding_completed),
            sign_in=sign_in_of(r),
        ))
    return out


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: NewUser,
    db: AsyncSession = Depends(get_db),
    _: user_schemas.User = Depends(require_admin),
):
    role = _valid_role(payload.role)
    email = payload.email.strip().lower()

    if (await db.execute(select(User.user_id).where(User.email == email))).first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A user already exists with email {email}.",
        )

    user = User(
        email=email,
        first_name=payload.first_name.strip(),
        last_name=payload.last_name.strip(),
        password=get_password_hash(payload.password),
        role=role,
        email_verified=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    return UserOut(
        user_id=user.user_id, email=user.email, first_name=user.first_name,
        last_name=user.last_name, role=role, groups=[],
    )


@router.patch("/users/{user_id}/role", response_model=UserOut)
async def change_role(
    user_id: int,
    payload: RoleChange,
    db: AsyncSession = Depends(get_db),
    current_user: user_schemas.User = Depends(require_admin),
):
    await _assert_may_act_on(db, current_user, user_id)

    # "SUPERADMIN" is not a core role: it is ADMIN plus a row in
    # admin_superadmin. Accepting it here keeps it one decision on the
    # screen instead of two controls that must be kept consistent.
    wants_super = payload.role.strip().upper() == "SUPERADMIN"
    role = UserRole.ADMIN.value if wants_super else _valid_role(payload.role)

    row = (await db.execute(select(User).where(User.user_id == user_id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such user.")

    was_super = (await db.execute(text(
        f"SELECT 1 FROM {_schema()}.admin_superadmin WHERE user_id = :uid"
    ), {"uid": user_id})).first() is not None

    # Only a superadmin may grant or revoke it. An org admin who could would
    # promote themselves straight out of their own boundary.
    if wants_super != was_super and not await is_superadmin(db, current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a superadministrator can grant or revoke that.",
        )

    # Losing your own superadmin status while you are the only one leaves the
    # install with nobody able to name another.
    if was_super and not wants_super and row.email == current_user.email:
        others = (await db.execute(text(
            f"SELECT count(*) FROM {_schema()}.admin_superadmin WHERE user_id <> :uid"
        ), {"uid": user_id})).scalar() or 0
        if others == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="You are the only superadministrator; name another one first.",
            )

    # Refusing self-demotion is not paternalism: an install whose last
    # administrator demotes themselves has no way back through this panel.
    if row.email == current_user.email and role != UserRole.ADMIN.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot remove your own administrator role.",
        )

    # Taken before the commit that expires it: reading an ORM attribute
    # afterwards makes SQLAlchemy reload the row, which is synchronous IO
    # on an async session — MissingGreenlet, and a 500 over a successful
    # write. The routes that must echo the new values call db.refresh()
    # instead; these two only need what they already hold.
    identity = {
        "user_id": row.user_id,
        "email": row.email,
        "first_name": row.first_name,
        "last_name": row.last_name,
    }

    await db.execute(update(User).where(User.user_id == user_id).values(role=role))

    if wants_super and not was_super:
        await db.execute(text(
            f"INSERT INTO {_schema()}.admin_superadmin (user_id) VALUES (:uid) "
            "ON CONFLICT DO NOTHING"
        ), {"uid": user_id})
    elif was_super and not wants_super:
        await db.execute(text(
            f"DELETE FROM {_schema()}.admin_superadmin WHERE user_id = :uid"
        ), {"uid": user_id})

    await db.commit()
    return UserOut(**identity, role=role, superadmin=wants_super, groups=[])


# --------------------------------------------------------------------------
# Groups
# --------------------------------------------------------------------------


async def _load_group(db: AsyncSession, group_id: int) -> GroupOut:
    row = (await db.execute(text(
        f"SELECT group_id, name, description FROM {_schema()}.admin_group "
        "WHERE group_id = :gid"
    ), {"gid": group_id})).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such group.")

    members = [r[0] for r in (await db.execute(text(
        f"SELECT user_id FROM {_schema()}.admin_group_member WHERE group_id = :gid "
        "ORDER BY user_id"
    ), {"gid": group_id})).all()]
    perms = [r[0] for r in (await db.execute(text(
        f"SELECT permission FROM {_schema()}.admin_group_permission WHERE group_id = :gid "
        "ORDER BY permission"
    ), {"gid": group_id})).all()]

    return GroupOut(
        group_id=row[0], name=row[1], description=row[2],
        members=members, permissions=perms,
    )


@router.get("/permissions")
async def list_permissions(_: user_schemas.User = Depends(require_admin)):
    """The catalogue this build can actually enforce — see permissions.py."""
    return list(PERMISSIONS)


@router.get("/groups", response_model=list[GroupOut])
async def list_groups(
    db: AsyncSession = Depends(get_db),
    _: user_schemas.User = Depends(require_admin),
):
    ids = [r[0] for r in (await db.execute(text(
        f"SELECT group_id FROM {_schema()}.admin_group ORDER BY name"
    ))).all()]
    return [await _load_group(db, gid) for gid in ids]


@router.post("/groups", response_model=GroupOut, status_code=status.HTTP_201_CREATED)
async def create_group(
    payload: GroupIn,
    db: AsyncSession = Depends(get_db),
    _: user_schemas.User = Depends(require_admin),
):
    name = payload.name.strip()
    if (await db.execute(text(
        f"SELECT 1 FROM {_schema()}.admin_group WHERE lower(name) = lower(:n)"
    ), {"n": name})).first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A group named {name!r} already exists.",
        )

    gid = (await db.execute(text(
        f"INSERT INTO {_schema()}.admin_group (name, description) "
        "VALUES (:n, :d) RETURNING group_id"
    ), {"n": name, "d": payload.description})).scalar()
    await db.commit()
    return await _load_group(db, gid)


@router.delete("/groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group(
    group_id: int,
    db: AsyncSession = Depends(get_db),
    _: user_schemas.User = Depends(require_admin),
):
    """Memberships and permissions go with it, by ON DELETE CASCADE. No user
    is touched: a group is a grouping, not an owner.
    """
    await _load_group(db, group_id)  # 404 before any write
    await db.execute(text(
        f"DELETE FROM {_schema()}.admin_group WHERE group_id = :gid"
    ), {"gid": group_id})
    await db.commit()


@router.put("/groups/{group_id}/permissions", response_model=GroupOut)
async def set_permissions(
    group_id: int,
    payload: PermissionsIn,
    db: AsyncSession = Depends(get_db),
    _: user_schemas.User = Depends(require_admin),
):
    """Replaces the whole set, rather than adding to it: the screen shows a
    list of checkboxes, and a PUT that only ever added would make unticking
    one impossible.
    """
    await _load_group(db, group_id)

    # A permission no code reads would display as a granted capability while
    # enforcing nothing. Refuse the whole call and name every offender.
    if unknown := unknown_permissions(payload.permissions):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown permission(s): {', '.join(unknown)}.",
        )

    await db.execute(text(
        f"DELETE FROM {_schema()}.admin_group_permission WHERE group_id = :gid"
    ), {"gid": group_id})
    for permission in sorted(set(payload.permissions)):
        await db.execute(text(
            f"INSERT INTO {_schema()}.admin_group_permission (group_id, permission) "
            "VALUES (:gid, :p)"
        ), {"gid": group_id, "p": permission})
    await db.commit()
    return await _load_group(db, group_id)


@router.post("/groups/{group_id}/members", response_model=GroupOut)
async def add_member(
    group_id: int,
    payload: MemberIn,
    db: AsyncSession = Depends(get_db),
    _: user_schemas.User = Depends(require_admin),
):
    await _load_group(db, group_id)
    await _assert_may_act_on(db, _, payload.user_id)
    if not (await db.execute(select(User.user_id).where(User.user_id == payload.user_id))).first():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such user.")

    # Idempotent: adding an existing member is not an error, and a 409 here
    # would make a double-click look like a failure.
    await db.execute(text(
        f"INSERT INTO {_schema()}.admin_group_member (group_id, user_id) "
        "VALUES (:gid, :uid) ON CONFLICT DO NOTHING"
    ), {"gid": group_id, "uid": payload.user_id})
    await db.commit()
    return await _load_group(db, group_id)


@router.delete("/groups/{group_id}/members/{user_id}", response_model=GroupOut)
async def remove_member(
    group_id: int,
    user_id: int,
    db: AsyncSession = Depends(get_db),
    _: user_schemas.User = Depends(require_admin),
):
    await _load_group(db, group_id)
    await _assert_may_act_on(db, _, user_id)
    await db.execute(text(
        f"DELETE FROM {_schema()}.admin_group_member "
        "WHERE group_id = :gid AND user_id = :uid"
    ), {"gid": group_id, "uid": user_id})
    await db.commit()
    return await _load_group(db, group_id)


# --------------------------------------------------------------------------
# Editing and closing an account
# --------------------------------------------------------------------------


class UserEdit(BaseModel):
    first_name: str | None = Field(default=None, min_length=1, max_length=100)
    last_name: str | None = Field(default=None, min_length=1, max_length=100)
    plan: str | None = Field(default=None, max_length=50)


@router.patch("/users/{user_id}", response_model=UserOut)
async def edit_user(
    user_id: int,
    payload: UserEdit,
    db: AsyncSession = Depends(get_db),
    _: user_schemas.User = Depends(require_admin),
):
    """Name and plan. The email is deliberately not editable: it is the key
    every ownership table joins on (`th2agents_store.owner_id`,
    `llm_usage.owner_id`), so changing it here would silently orphan an
    account's agents and its whole consumption history.
    """
    await _assert_may_act_on(db, _, user_id)
    row = (await db.execute(select(User).where(User.user_id == user_id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such user.")

    values = {k: v for k, v in payload.model_dump(exclude_none=True).items()}
    if values:
        await db.execute(update(User).where(User.user_id == user_id).values(**values))
        await db.commit()
        await db.refresh(row)

    return UserOut(
        user_id=row.user_id, email=row.email, first_name=row.first_name,
        last_name=row.last_name,
        role=row.role.value if hasattr(row.role, "value") else str(row.role),
        plan=row.plan,
    )


async def _what_the_account_holds(db: AsyncSession, user_id: int, email: str) -> dict[str, int]:
    """Everything that would be orphaned by deleting this account.

    Checking agents alone is not enough: an account with no agent still held
    2 integrations, 4 saved API keys and 8 tool configs on dev. Deleting it
    would have destroyed them silently.
    """
    schema = _schema()
    counts: dict[str, int] = {}
    for label, sql, param in (
        ("agents", f"SELECT count(*) FROM {schema}.th2agents_store WHERE owner_id = :v", email),
        ("llm_usage", f"SELECT count(*) FROM {schema}.llm_usage WHERE owner_id = :v", email),
        ("integrations", f"SELECT count(*) FROM {schema}.integrations WHERE user_id = :v", user_id),
        ("saved_api_keys", f"SELECT count(*) FROM {schema}.saved_api_keys WHERE owner_id = :v", email),
        ("tool_configs", f"SELECT count(*) FROM {schema}.tool_configs WHERE owner_id = :v", email),
    ):
        try:
            n = (await db.execute(text(sql), {"v": param})).scalar() or 0
        except Exception:  # noqa: BLE001 -- an absent table is not a reason to block
            n = 0
        if n:
            counts[label] = n
    return counts


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: user_schemas.User = Depends(require_admin),
):
    """Refuses on anything that would be left dangling, and says what.

    Deletion is the one irreversible act on this screen, so it only goes
    through for an account that holds nothing. An account that owns agents,
    keys or integrations has to be emptied deliberately first — the panel
    will not decide for you what happens to someone else's work.
    """
    await _assert_may_act_on(db, current_user, user_id)
    row = (await db.execute(select(User).where(User.user_id == user_id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such user.")

    if row.email == current_user.email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot delete your own account.",
        )

    if str(row.role.value if hasattr(row.role, "value") else row.role) == UserRole.ADMIN.value:
        remaining = (await db.execute(text(
            f"SELECT count(*) FROM {_schema()}.\"user\" "
            "WHERE role::text = 'ADMIN' AND user_id <> :uid"
        ), {"uid": user_id})).scalar() or 0
        if remaining == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This is the last administrator; deleting it locks everyone out.",
            )

    holds = await _what_the_account_holds(db, user_id, row.email)
    if holds:
        detail = ", ".join(f"{n} {label}" for label, n in holds.items())
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"This account still holds {detail}. Move or delete them first — "
                "removing the account would orphan them."
            ),
        )

    await db.execute(text(
        f"DELETE FROM {_schema()}.admin_group_member WHERE user_id = :uid"
    ), {"uid": user_id})
    await db.execute(delete(User).where(User.user_id == user_id))
    await db.commit()


# --------------------------------------------------------------------------
# Demands an administrator can make of an account
# --------------------------------------------------------------------------


@router.post("/users/{user_id}/password-reset", status_code=status.HTTP_202_ACCEPTED)
async def demand_password_reset(
    user_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: user_schemas.User = Depends(require_admin),
):
    """Sends the user the core's own reset link.

    The password is not changed here and no temporary one is minted: only
    the person receiving the email ever picks it. An administrator who could
    set someone's password could sign in as them.
    """
    await _assert_may_act_on(db, _, user_id)
    row = (await db.execute(select(User).where(User.user_id == user_id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such user.")

    from apowerb.auth.service import request_password_reset

    await request_password_reset(row.email, db, str(request.base_url).rstrip("/"))
    return {"sent_to": row.email}


@router.post("/users/{user_id}/require-email-verification", status_code=status.HTTP_202_ACCEPTED)
async def demand_email_verification(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    _: user_schemas.User = Depends(require_admin),
):
    """Marks the address unverified again and re-sends the link.

    Both steps, in that order: `send_verification_email` is a no-op on an
    already-verified address, so re-sending alone would do nothing at all.
    """
    await _assert_may_act_on(db, _, user_id)
    row = (await db.execute(select(User).where(User.user_id == user_id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such user.")

    # Taken before the commit that expires it: reading an ORM attribute
    # afterwards makes SQLAlchemy reload the row, which is synchronous IO
    # on an async session — MissingGreenlet, and a 500 over a successful
    # write. The routes that must echo the new values call db.refresh()
    # instead; these two only need what they already hold.
    email = row.email

    await db.execute(
        update(User).where(User.user_id == user_id).values(email_verified=False)
    )
    await db.commit()

    from apowerb.auth.service import send_verification_email

    await send_verification_email(email, db)
    return {"sent_to": email}


@router.post("/users/{user_id}/force-relogin", response_model=UserOut)
async def force_relogin(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    _: user_schemas.User = Depends(require_admin),
):
    """Invalidate every token this account currently holds.

    Writes the cut-off the core consults on each request; nothing is
    re-implemented here. Takes effect on the next call — including the
    refresh cookie, so signing back in is the only way through.

    Scheduled agent runs are untouched: they carry `agent_refresh` tokens,
    which never unlock user-scope endpoints and are not what an
    administrator means by "make this person sign in again".
    """
    await _assert_may_act_on(db, _, user_id)
    row = (await db.execute(select(User).where(User.user_id == user_id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such user.")

    identity = {
        "user_id": row.user_id,
        "email": row.email,
        "first_name": row.first_name,
        "last_name": row.last_name,
        "role": row.role.value if hasattr(row.role, "value") else str(row.role),
    }

    await db.execute(
        update(User)
        .where(User.user_id == user_id)
        .values(sessions_valid_from=datetime.now(timezone.utc))
    )
    await db.commit()
    return UserOut(**identity)


class MfaDemand(BaseModel):
    required: bool


@router.post("/users/{user_id}/require-mfa", response_model=UserOut)
async def set_mfa_required(
    user_id: int,
    payload: MfaDemand,
    db: AsyncSession = Depends(get_db),
    _: user_schemas.User = Depends(require_admin),
):
    """Demand a second factor from this account, or stop demanding it.

    Enabling MFA *for* someone remains impossible by construction — the
    secret is born when they scan the QR code, and a secret an
    administrator knows is not a second factor. What an administrator can
    do is refuse them the product until they have one, which is what this
    sets. The core lets them reach the enrolment routes and nothing else.
    """
    await _assert_may_act_on(db, _, user_id)
    row = (await db.execute(select(User).where(User.user_id == user_id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such user.")

    identity = {
        "user_id": row.user_id,
        "email": row.email,
        "first_name": row.first_name,
        "last_name": row.last_name,
        "role": row.role.value if hasattr(row.role, "value") else str(row.role),
        "mfa_enabled": bool(row.mfa_enabled),
    }

    await db.execute(
        update(User).where(User.user_id == user_id).values(mfa_required=payload.required)
    )
    await db.commit()
    return UserOut(**identity, mfa_required=payload.required)

@router.post("/users/{user_id}/disable-mfa", response_model=UserOut)
async def disable_mfa(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    _: user_schemas.User = Depends(require_admin),
):
    """Clears the second factor — the locked-out case, somebody lost their
    phone. Enabling it *for* someone is impossible by construction: the
    secret is generated when they scan the QR code, and a secret an
    administrator knows is not a second factor.
    """
    await _assert_may_act_on(db, _, user_id)
    row = (await db.execute(select(User).where(User.user_id == user_id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such user.")

    await db.execute(update(User).where(User.user_id == user_id).values(
        mfa_enabled=False, mfa_secret=None, mfa_backup_codes=None,
    ))
    await db.commit()
    await db.refresh(row)

    return UserOut(
        user_id=row.user_id, email=row.email, first_name=row.first_name,
        last_name=row.last_name,
        role=row.role.value if hasattr(row.role, "value") else str(row.role),
        mfa_enabled=False,
    )


# --------------------------------------------------------------------------
# Organisations
# --------------------------------------------------------------------------
#
# Creating, renaming, deleting an organisation and assigning a user to one
# live in the `th2agent-organizations` extension, not here. Partitioning a
# platform between tenants governs other people's reach rather than serving
# the person who runs it, which is the line this build draws.
#
# What stays is the machinery, and it stays on purpose: `administered_user_ids`
# in guard.py still bounds what an administrator sees, the two tables are
# still created, and `UserOut.organization` is still filled. An install with
# no organisations lands on the safe end of that guard -- an admin who is not
# a superadmin administers only himself -- so removing it would not simplify
# the core, it would widen it.

@router.get("/me")
async def who_am_i(
    db: AsyncSession = Depends(get_db),
    current_user: user_schemas.User = Depends(require_admin),
):
    """What this administrator may do, so the screen can stop guessing.

    Without it the front would have to infer the boundary from what it
    happens to receive, and a filtered list looks exactly like a small one.
    """
    superadmin = await is_superadmin(db, current_user)
    org = (await db.execute(text(
        f"SELECT o.org_id, o.name FROM {_schema()}.admin_org_member m "
        f"JOIN {_schema()}.admin_organization o ON o.org_id = m.org_id "
        f'JOIN {_schema()}."user" u ON u.user_id = m.user_id '
        "WHERE lower(u.email) = lower(:email)"
    ), {"email": current_user.email})).first()
    return {
        "email": current_user.email,
        "superadmin": superadmin,
        "organization": {"org_id": org[0], "name": org[1]} if org else None,
    }

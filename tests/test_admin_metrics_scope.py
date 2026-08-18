"""The dashboard is scoped, and it is scoped *in the query*.

An aggregate is the easiest place for a boundary to disappear: the numbers
still look plausible, and nobody can tell by reading the screen that a
count included an organisation they administer none of. So these assert on
the SQL that goes out — every statement touching usage must carry the
owner filter — rather than on the shape of the answer.
"""

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("ENCRYPT_KEY", "test-only-key-not-used-anywhere-else")

from apowerb.admin import router as router_module  # noqa: E402


def _admin(email="org@example.com"):
    u = MagicMock()
    u.role = "ADMIN"
    u.email = email
    return u


def _db_recording(statements, *, emails=("a@example.com",)):
    """A db whose every execute() is captured, answering plausibly.

    `first()` returns a 4-tuple because the totals query reads four
    columns; `scalars().all()` returns the member emails.
    """
    db = AsyncMock()

    async def execute(stmt, params=None):
        statements.append((str(stmt), params or {}))
        result = MagicMock()
        result.first.return_value = (0, 0, 0, 0)
        result.scalar.return_value = 0
        result.all.return_value = []
        result.scalars.return_value.all.return_value = list(emails)
        return result

    db.execute = AsyncMock(side_effect=execute)
    return db


@pytest.mark.asyncio
async def test_an_org_admin_never_queries_usage_unfiltered():
    statements = []
    db = _db_recording(statements)

    with patch(
        "apowerb.admin.router.administered_user_ids",
        new=AsyncMock(return_value={3, 9}),
    ):
        await router_module.platform_metrics(days=30, db=db, _=_admin())

    # The invariant is the bound, not the column it is spelled on: usage is
    # keyed by owner_id, sessions by user_id, and the "never used" count
    # scopes on the user table itself. What must never happen is a statement
    # reaching either table with no email bound at all.
    touching = [
        (sql, params)
        for sql, params in statements
        if "llm_usage" in sql or ".sessions" in sql
    ]
    assert touching, "aucune requete sur l usage"
    for sql, params in touching:
        assert "ANY(:emails)" in sql, f"requete non bornee : {sql}"
        assert params.get("emails") == ["a@example.com"]


@pytest.mark.asyncio
async def test_a_superadmin_is_not_filtered():
    statements = []
    db = _db_recording(statements)

    with patch(
        "apowerb.admin.router.administered_user_ids",
        new=AsyncMock(return_value=None),
    ):
        out = await router_module.platform_metrics(days=30, db=db, _=_admin())

    assert out.scope == "platform"
    assert not any("ANY(:emails)" in sql for sql, _ in statements)


@pytest.mark.asyncio
async def test_an_admin_with_nobody_to_administer_reports_nothing():
    """An empty scope must not become `= ANY('{}')`, which reads to a human
    as "no filter" and is one refactor away from being one.
    """
    statements = []
    db = _db_recording(statements, emails=())

    with patch(
        "apowerb.admin.router.administered_user_ids",
        new=AsyncMock(return_value=set()),
    ):
        out = await router_module.platform_metrics(days=30, db=db, _=_admin())

    assert out.scope == "organization"
    assert out.totals.users == 0
    assert out.daily == []
    assert not any("llm_usage" in sql for sql, _ in statements)


@pytest.mark.asyncio
async def test_the_window_covers_every_day_including_the_empty_ones():
    """A line drawn only through the days that have data hides how quiet the
    others were.
    """
    statements = []
    db = _db_recording(statements)

    with patch(
        "apowerb.admin.router.administered_user_ids",
        new=AsyncMock(return_value=None),
    ):
        out = await router_module.platform_metrics(days=7, db=db, _=_admin())

    assert len(out.daily) == 8
    assert all(point.calls == 0 for point in out.daily)
    days = [point.day for point in out.daily]
    assert days == sorted(days)


@pytest.mark.asyncio
@pytest.mark.parametrize("asked,expected", [(0, 1), (-5, 1), (10_000, 365)])
async def test_the_window_is_bounded(asked, expected):
    statements = []
    db = _db_recording(statements)
    with patch(
        "apowerb.admin.router.administered_user_ids",
        new=AsyncMock(return_value=None),
    ):
        out = await router_module.platform_metrics(days=asked, db=db, _=_admin())
    assert out.window_days == expected


@pytest.mark.asyncio
async def test_each_table_gets_the_kind_of_timestamp_its_column_stores():
    """asyncpg refuses an aware datetime for a `timestamp WITHOUT time zone`
    outright — "can't subtract offset-naive and offset-aware datetimes" —
    and the whole dashboard answers 500. The mocks here never reach the
    driver, so the binding itself is what gets asserted.
    """
    statements = []
    db = _db_recording(statements)

    with patch(
        "apowerb.admin.router.administered_user_ids",
        new=AsyncMock(return_value=None),
    ):
        await router_module.platform_metrics(days=30, db=db, _=_admin())

    usage = [p for sql, p in statements if "llm_usage" in sql and "since" in p]
    sessions = [p for sql, p in statements if ".sessions" in sql and "since" in p]
    assert usage and sessions, "les deux tables doivent etre interrogees"

    for params in usage:
        assert params["since"].tzinfo is not None, (
            "llm_usage.created_at est WITH time zone"
        )
    for params in sessions:
        assert params["since"].tzinfo is None, (
            "sessions.create_time est WITHOUT time zone"
        )

    # Same instant on both sides, only the tzinfo differs.
    assert usage[0]["since"].replace(tzinfo=None) == sessions[0]["since"]

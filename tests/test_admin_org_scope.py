"""Who administers whom — the boundary, not the plumbing.

`role` stays {ADMIN, USER} in the core: adding SUPERADMIN to `role_enum`
would mean altering a type shared by six schemas on one instance,
production included, and enum values cannot be removed. So a superadmin is
an ADMIN listed in `admin_superadmin`, and an ADMIN who is not listed
administers only their own organisation.
"""

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("ENCRYPT_KEY", "test-only-key-not-used-anywhere-else")

from apowerb.admin.guard import administered_user_ids, is_superadmin  # noqa: E402


def _admin(email="boss@example.com"):
    u = MagicMock()
    u.role = "ADMIN"
    u.email = email
    return u


def _db(*results):
    """A session whose `execute` is awaited and whose Result is synchronous.

    `AsyncMock()` alone makes `scalar()`/`first()` return coroutines, which a
    real SQLAlchemy Result never does — the mock would disagree with the
    library it stands in for.
    """
    calls = iter(results)

    async def execute(*_a, **_k):
        value = next(calls)
        result = MagicMock()
        result.scalar.return_value = value
        result.first.return_value = (value,) if value is not None else None
        result.scalars.return_value.all.return_value = value if isinstance(value, list) else []
        return result

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=execute)
    return db


@pytest.mark.asyncio
async def test_a_plain_user_is_never_a_superadmin():
    user = MagicMock(role="USER", email="someone@example.com")
    assert await is_superadmin(_db(0), user) is False


@pytest.mark.asyncio
async def test_every_admin_is_superadmin_until_one_is_named():
    """Otherwise the first organisation could never be created: the table
    that grants the right can only be written by someone who has it."""
    assert await is_superadmin(_db(0), _admin()) is True


@pytest.mark.asyncio
async def test_once_named_the_others_are_not():
    # count = 1, then the membership lookup finds nothing for this admin.
    db = _db(1, None)
    assert await is_superadmin(db, _admin("other@example.com")) is False


@pytest.mark.asyncio
async def test_a_superadmin_is_unrestricted():
    """None means no filter at all — the same convention the evaluation
    service uses for an admin."""
    assert await administered_user_ids(_db(0), _admin()) is None


@pytest.mark.asyncio
async def test_an_org_admin_only_sees_their_organisation():
    # not superadmin (count=1, not listed), org 7, own id 3, members [3, 9]
    db = _db(1, None, 7, 3, [3, 9])
    assert await administered_user_ids(db, _admin("org@example.com")) == {3, 9}


@pytest.mark.asyncio
async def test_an_admin_with_no_organisation_administers_only_themselves():
    """A scope that resolves to "everyone" on missing data is how a boundary
    silently stops being one."""
    db = _db(1, None, None, 42)
    assert await administered_user_ids(db, _admin("lonely@example.com")) == {42}

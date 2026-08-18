"""The router's own rules, the ones a mock would happily let through."""

import pytest
from fastapi import HTTPException
from unittest.mock import AsyncMock, MagicMock

from apowerb.models import UserRole
from apowerb.admin import router as router_module


def _db_returning(**result_attrs):
    """A session whose `execute` is awaited and whose Result is synchronous.

    `AsyncMock()` alone makes `result.first()` / `.scalar_one_or_none()`
    return coroutines, which a real SQLAlchemy Result never does — the mock
    would then disagree with the library it stands in for.
    """
    result = MagicMock()
    for name, value in result_attrs.items():
        getattr(result, name).return_value = value
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    return db


def _admin(email="boss@example.com"):
    u = MagicMock()
    u.role = UserRole.ADMIN.value
    u.email = email
    return u


def test_a_role_is_accepted_in_any_casing_and_stored_canonically():
    """The enum is upper case and Postgres rejects anything else, so lower
    case must be normalised on the way in rather than reaching the database.
    """
    assert router_module._valid_role("admin") == "ADMIN"
    assert router_module._valid_role(" User ") == "USER"


def test_an_unknown_role_is_a_400_naming_what_is_allowed():
    with pytest.raises(HTTPException) as exc:
        router_module._valid_role("superuser")
    assert exc.value.status_code == 400
    assert "ADMIN" in exc.value.detail and "USER" in exc.value.detail


@pytest.mark.asyncio
async def test_an_admin_cannot_strip_their_own_admin_role():
    """An install whose last admin demotes themselves has no way back in
    through this panel. Refusing is recoverability, not paternalism.
    """
    me = _admin("me@example.com")
    row = MagicMock(user_id=1, email="me@example.com", first_name="A", last_name="B")

    db = _db_returning(scalar_one_or_none=row)

    with pytest.raises(HTTPException) as exc:
        await router_module.change_role(
            user_id=1,
            payload=router_module.RoleChange(role="USER"),
            db=db,
            current_user=me,
        )
    assert exc.value.status_code == 400
    # And nothing was written.
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_demoting_someone_else_is_allowed():
    other = MagicMock(user_id=2, email="other@example.com", first_name="C", last_name="D")
    db = _db_returning(scalar_one_or_none=other)

    out = await router_module.change_role(
        user_id=2,
        payload=router_module.RoleChange(role="USER"),
        db=db,
        current_user=_admin("me@example.com"),
    )
    assert out.role == "USER"
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_a_new_user_password_is_hashed_never_stored_as_typed():
    """The panel takes a password from the administrator. What reaches the
    database must not be it, and no response may echo it back.
    """
    db = _db_returning(first=None)  # no user with that email yet
    db.add = MagicMock()

    # A real refresh loads back what the database assigned — without it the
    # row has no id, which is exactly what the response model needs.
    async def _refresh(obj):
        obj.user_id = 42

    db.refresh = AsyncMock(side_effect=_refresh)

    payload = router_module.NewUser(
        email="New.Person@Example.com", first_name="New", last_name="Person",
        password="a-real-secret-42", role="user",
    )
    out = await router_module.create_user(payload=payload, db=db, _=_admin())

    stored = db.add.call_args.args[0]
    assert stored.password != "a-real-secret-42"
    assert stored.password.startswith("$")          # a hash, not the plaintext
    assert stored.email == "new.person@example.com"  # normalised
    assert stored.role == "USER"
    assert "a-real-secret-42" not in out.model_dump_json()

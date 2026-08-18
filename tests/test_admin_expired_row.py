"""A row that expires on commit, like the real one does.

Every test in this package mocks the session with plain `AsyncMock`s,
whose attributes answer happily forever. The real `AsyncSession` does not:
`commit()` expires every loaded instance, and the next attribute read asks
SQLAlchemy to reload it — synchronous IO on an async session, which raises
`MissingGreenlet`. Four routes built their response that way and answered
500 over a write that had already succeeded.

So the double here reproduces the one behaviour that matters: after
`commit()`, touching the row raises. A route that snapshots what it needs
beforehand passes; a route that reads afterwards fails the way it fails in
production.
"""

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import MissingGreenlet

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("ENCRYPT_KEY", "test-only-key-not-used-anywhere-else")

from apowerb.admin import router as router_module  # noqa: E402


class ExpiringRow:
    """Answers until the session commits, then raises like SQLAlchemy."""

    def __init__(self, session, **fields):
        object.__setattr__(self, "_session", session)
        object.__setattr__(self, "_fields", fields)

    def __getattr__(self, name):
        session = object.__getattribute__(self, "_session")
        if session.committed:
            raise MissingGreenlet(
                "greenlet_spawn has not been called; can't call await_only() here."
            )
        fields = object.__getattribute__(self, "_fields")
        if name not in fields:
            raise AttributeError(name)
        return fields[name]


class FakeSession:
    """Just enough session, with the expiry that the mocks all forget."""

    def __init__(self, row=None, scalars=None):
        self.committed = False
        self.row = row
        self._scalars = scalars or {}

    async def execute(self, statement, params=None):
        result = MagicMock()
        text = str(statement)
        result.scalar_one_or_none.return_value = self.row
        result.first.return_value = None if "admin_superadmin" in text else (0,)
        result.scalar.return_value = self._scalars.get("count", 0)
        result.all.return_value = []
        result.scalars.return_value.all.return_value = []
        return result

    async def commit(self):
        self.committed = True

    async def refresh(self, row):
        """Reloads the instance — the legitimate way to read after a commit.

        Modelled because three routes rely on it and are right to: a double
        that only knew about expiry would condemn correct code.
        """
        self.committed = False


def _actor(email="boss@example.com"):
    u = MagicMock()
    u.role = "ADMIN"
    u.email = email
    return u


def _row(session):
    return ExpiringRow(
        session,
        user_id=7,
        email="target@example.com",
        first_name="Ta",
        last_name="Rget",
        role="USER",
        plan="free",
    )


@pytest.mark.asyncio
async def test_change_role_does_not_read_the_row_after_committing():
    db = FakeSession()
    db.row = _row(db)

    with patch(
        "apowerb.admin.router.administered_user_ids", new=AsyncMock(return_value=None)
    ), patch(
        "apowerb.admin.router.is_superadmin", new=AsyncMock(return_value=True)
    ):
        out = await router_module.change_role(
            user_id=7,
            payload=router_module.RoleChange(role="ADMIN"),
            db=db,
            current_user=_actor(),
        )

    assert out.email == "target@example.com"
    assert out.role == "ADMIN"


@pytest.mark.asyncio
async def test_edit_user_reads_the_row_only_through_a_refresh():
    db = FakeSession()
    db.row = _row(db)

    with patch(
        "apowerb.admin.router.administered_user_ids", new=AsyncMock(return_value=None)
    ):
        out = await router_module.edit_user(
            user_id=7,
            payload=router_module.UserEdit(first_name="New"),
            db=db,
            _=_actor(),
        )

    assert out.user_id == 7


@pytest.mark.asyncio
async def test_disable_mfa_reads_the_row_only_through_a_refresh():
    db = FakeSession()
    db.row = _row(db)

    with patch(
        "apowerb.admin.router.administered_user_ids", new=AsyncMock(return_value=None)
    ):
        out = await router_module.disable_mfa(user_id=7, db=db, _=_actor())

    assert out.email == "target@example.com"


@pytest.mark.asyncio
async def test_email_verification_does_not_read_the_row_after_committing():
    db = FakeSession()
    db.row = _row(db)
    sent = {}

    async def fake_send(email, _db):
        sent["to"] = email

    with patch(
        "apowerb.admin.router.administered_user_ids", new=AsyncMock(return_value=None)
    ), patch("apowerb.auth.service.send_verification_email", new=fake_send):
        out = await router_module.demand_email_verification(user_id=7, db=db, _=_actor())

    assert out == {"sent_to": "target@example.com"}
    assert sent["to"] == "target@example.com"

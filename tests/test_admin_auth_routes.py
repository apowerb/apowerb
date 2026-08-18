"""The two demands write state and nothing else.

Both routes are thin on purpose: the enforcement lives in the core, on the
path of every request. What is worth pinning here is that they are scoped
like every other acting route, that they read the row *before* the commit
that expires it, and that "stop demanding" is reachable — a flag you can
set and not clear is a trap.
"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import MissingGreenlet

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("ENCRYPT_KEY", "test-only-key-not-used-anywhere-else")

from apowerb.admin import router as router_module  # noqa: E402


class ExpiringRow:
    """Raises after the session commits, like a real ORM instance."""

    def __init__(self, session, **fields):
        object.__setattr__(self, "_session", session)
        object.__setattr__(self, "_fields", fields)

    def __getattr__(self, name):
        session = object.__getattribute__(self, "_session")
        if session.committed:
            raise MissingGreenlet("greenlet_spawn has not been called")
        fields = object.__getattribute__(self, "_fields")
        if name not in fields:
            raise AttributeError(name)
        return fields[name]


class FakeSession:
    def __init__(self):
        self.committed = False
        self.row = None
        self.writes = []

    async def execute(self, statement, params=None):
        rendered = str(statement)
        if rendered.lstrip().upper().startswith("UPDATE"):
            self.writes.append(rendered)
        result = MagicMock()
        result.scalar_one_or_none.return_value = self.row
        result.first.return_value = None
        result.scalar.return_value = 0
        result.all.return_value = []
        return result

    async def commit(self):
        self.committed = True


def _admin():
    u = MagicMock()
    u.role = "ADMIN"
    u.email = "boss@example.com"
    return u


def _session_with_row(**over):
    db = FakeSession()
    fields = {
        "user_id": 7, "email": "target@example.com", "first_name": "Ta",
        "last_name": "Rget", "role": "USER", "mfa_enabled": False,
    }
    fields.update(over)
    db.row = ExpiringRow(db, **fields)
    return db


@pytest.mark.asyncio
async def test_force_relogin_writes_a_cutoff_and_answers_after_the_commit():
    db = _session_with_row()

    with patch("apowerb.admin.router.administered_user_ids", new=AsyncMock(return_value=None)):
        out = await router_module.force_relogin(user_id=7, db=db, _=_admin())

    assert out.email == "target@example.com"
    assert db.committed
    # The cut-off column is the one written — the core reads exactly that.
    assert any("sessions_valid_from" in w for w in db.writes), db.writes
    # Answering at all is the point: the row is expired by then, and reading
    # it after the commit is what turned a successful write into a 500 once.


@pytest.mark.asyncio
async def test_requiring_a_second_factor_can_be_undone():
    """A flag you can set and not clear is a trap."""
    for demanded in (True, False):
        db = _session_with_row()
        with patch(
            "apowerb.admin.router.administered_user_ids", new=AsyncMock(return_value=None)
        ):
            out = await router_module.set_mfa_required(
                user_id=7,
                payload=router_module.MfaDemand(required=demanded),
                db=db,
                _=_admin(),
            )
        assert out.mfa_required is demanded


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["force_relogin", "set_mfa_required"])
async def test_both_refuse_a_target_outside_the_organisation(route):
    db = _session_with_row()
    kwargs = {"user_id": 999, "db": db, "_": _admin()}
    if route == "set_mfa_required":
        kwargs["payload"] = router_module.MfaDemand(required=True)

    with patch(
        "apowerb.admin.router.administered_user_ids", new=AsyncMock(return_value={3, 9})
    ):
        with pytest.raises(HTTPException) as exc:
            await getattr(router_module, route)(**kwargs)

    assert exc.value.status_code == 404

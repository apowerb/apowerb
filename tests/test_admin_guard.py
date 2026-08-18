"""Every route is admin-only, and "admin" is spelled the way auth sends it.

The core compared `role` against "admin" while auth fills it from
`UserRole.value` — "ADMIN" — so its bypass never fired (apowerb#70). This
extension reuses that fixed helper rather than re-deriving the test, and
these tests assert on the value auth actually produces, not on a spelling
convenient for the mock.
"""

import pytest
from fastapi import HTTPException
from unittest.mock import MagicMock

from apowerb.models import UserRole
from apowerb.admin.guard import require_admin


def _user(role):
    u = MagicMock()
    u.role = role
    u.email = "someone@example.com"
    return u


@pytest.mark.asyncio
async def test_an_admin_passes_on_the_value_auth_sends():
    assert UserRole.ADMIN.value == "ADMIN"
    user = _user(UserRole.ADMIN.value)
    assert await require_admin(current_user=user) is user


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["USER", "user", "User", "", None])
async def test_everyone_else_gets_403_not_404(role):
    """403, not 404: hiding the route from an authenticated user buys nothing
    and turns a permissions problem into a debugging one."""
    with pytest.raises(HTTPException) as exc:
        await require_admin(current_user=_user(role))
    assert exc.value.status_code == 403

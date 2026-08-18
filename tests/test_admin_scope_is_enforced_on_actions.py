"""Acting on a user is scoped, not just listing them.

`administered_user_ids` filtered the user list, so an org admin saw only
their own people — and every route that acts took its target from the path
and never asked. Knowing an id was enough to delete, demote, reset the
password of or clear the MFA of anyone on the platform.

A boundary that holds only for someone navigating through the UI is not a
boundary.
"""

import os
import sys
from pathlib import Path

import apowerb.admin.router
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

os.environ.setdefault("ENCRYPT_KEY", "test-only-key-not-used-anywhere-else")

from apowerb.admin import router as router_module  # noqa: E402


def _org_admin(email="org@example.com"):
    u = MagicMock()
    u.role = "ADMIN"
    u.email = email
    return u


@pytest.mark.asyncio
@pytest.mark.parametrize("outside", [999])
async def test_an_org_admin_cannot_act_on_someone_outside_their_organisation(outside):
    """404, not 403: confirming an id exists outside your scope already tells
    you something about an organisation you administer none of."""
    db = AsyncMock()

    with patch(
        "apowerb.admin.router.administered_user_ids",
        new=AsyncMock(return_value={3, 9}),
    ):
        with pytest.raises(HTTPException) as exc:
            await router_module._assert_may_act_on(db, _org_admin(), outside)

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_an_org_admin_may_act_within_their_organisation():
    db = AsyncMock()
    with patch(
        "apowerb.admin.router.administered_user_ids",
        new=AsyncMock(return_value={3, 9}),
    ):
        # No exception is the assertion.
        await router_module._assert_may_act_on(db, _org_admin(), 9)


@pytest.mark.asyncio
async def test_a_superadmin_is_not_bounded():
    db = AsyncMock()
    with patch(
        "apowerb.admin.router.administered_user_ids",
        new=AsyncMock(return_value=None),
    ):
        await router_module._assert_may_act_on(db, _org_admin(), 12345)


def test_every_route_taking_a_user_id_checks_the_scope():
    """The count is the point: this failed once because seven routes out of
    eight took a target from the path and never asked whose it was."""
    source = (
        Path(apowerb.admin.router.__file__)
    ).read_text(encoding="utf-8")

    guarded = source.count("await _assert_may_act_on(")
    assert guarded >= 8, (
        f"only {guarded} routes check the scope; every route acting on a "
        "user_id must, or the boundary holds for the UI alone"
    )

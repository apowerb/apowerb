"""Organisation management left the core on 19/08/2026.

Deciding who lands in which tenant governs other people's reach rather than
serving whoever installs the product, so the open-source build must not
answer on those routes at all. Same rule as supervision the day before, and
the same shape of test -- see ``test_supervision_left_the_core.py``.

**Only the capability left. The mechanism stayed, and that is deliberate.**
``administered_user_ids`` still bounds an administrator to his organisation,
the two tables are still created, and ``UserOut.organization`` is still
filled. Taking those out would not have simplified the core: with nothing to
belong to, an administrator who is not a superadmin administers only himself
-- the safe end of that guard -- whereas dropping the guard would have
widened him to the whole platform. The second half of this file is what
stops a later cleanup from "finishing the job" and silently doing that.

These tests ask the application, not its route table: this FastAPI version
expands ``include_router`` lazily, so a structural assertion would pass for
the wrong reason. A 401 on a route that exists is what proves a 404 means
*absent*.
"""

from __future__ import annotations

import inspect

from fastapi.testclient import TestClient

from apowerb.admin import guard, migration
from apowerb.main import app

client = TestClient(app, raise_server_exceptions=False)

# Exists and requires authentication: its 401 is the positive control that
# the application is wired at all, so a 404 next door is real absence.
A_ROUTE_THAT_STAYED = "/api/agents"

GONE = [
    ("get", "/api/admin/organizations"),
    ("post", "/api/admin/organizations"),
    ("patch", "/api/admin/organizations/1"),
    ("delete", "/api/admin/organizations/1"),
    ("put", "/api/admin/organizations/1/members/2"),
]


def test_a_core_route_still_answers():
    assert client.get(A_ROUTE_THAT_STAYED).status_code == 401


def test_the_control_panel_itself_stayed():
    """Only this corner of it is sold. A 401, not a 404."""
    assert client.get("/api/admin/users").status_code == 401
    assert client.get("/api/admin/groups").status_code == 401


def test_the_core_answers_404_on_every_organisation_route():
    """Not hidden -- absent. Installing the brick is what brings them back."""
    for method, path in GONE:
        got = getattr(client, method)(path)
        assert got.status_code == 404, f"{method.upper()} {path} -> {got.status_code}"


def test_the_bound_on_an_administrator_stayed():
    """The security half. An organisation narrows what an admin reaches, and
    that narrowing is not for sale -- only drawing the boundary is."""
    assert callable(guard.administered_user_ids)
    assert callable(guard.require_superadmin), (
        "the brick that now owns organisations depends on this one"
    )


def test_the_two_tables_are_still_the_core_s_to_create():
    """The brick owns the API, never the storage. Were this to move, one
    schema would have two owners and the second to run would be the one
    nobody reads."""
    source = inspect.getsource(migration.ensure_admin_tables)
    assert "admin_organization" in source
    assert "admin_org_member" in source


def test_a_user_still_carries_the_organisation_he_belongs_to():
    """Read stays, write goes. The commercial UI talks to this same core."""
    from apowerb.admin.router import UserOut

    assert "organization" in UserOut.model_fields
    assert "organization_id" in UserOut.model_fields

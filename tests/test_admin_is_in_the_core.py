"""The control panel is part of the open-source core since 18/08/2026.

It shipped as a private extension on the terms Farid set on 12/08 -- internal
for now, public later. This is that later, and the move needed no untangling:
the extension used exactly one core extension point.

Two things the move had to settle, and these tests pin both.

**The name collision.** Two functions were called `ensure_default_superadmin`,
both driven by `DEFAULT_SUPERADMIN_EMAIL`, doing different things: the core's
sets `role = ADMIN` on the user row, the panel's writes a row in
`admin_superadmin`. Under one roof they need distinct names, or one silently
shadows the other.

**The inversion.** `supervision_scope` existed because the core could not name
a superadmin. It can now, so it answers instead of waiting for a brick -- and
the supervision screen, which stayed commercial, reads that answer.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apowerb.core.extensions.registry import registry
from apowerb.main import app

client = TestClient(app, raise_server_exceptions=False)


def test_the_core_serves_the_control_panel():
    """401, not 404: the route is here and it is guarded."""
    assert client.get("/api/admin/users").status_code == 401


def test_a_route_that_never_moved_still_answers():
    """Positive control -- without it a 401 everywhere would look like success."""
    assert client.get("/api/agents").status_code == 401


def test_the_two_superadmin_bootstraps_kept_distinct_names():
    """Same environment variable, two different gestures. One name for both
    would have let whichever imported last win, silently."""
    from apowerb.admin import migration
    from apowerb.helpers import default_superadmin

    assert hasattr(migration, "ensure_superadmin_grant")
    assert not hasattr(migration, "ensure_default_superadmin")
    assert hasattr(default_superadmin, "ensure_default_superadmin")


def test_the_core_answers_the_supervision_question_itself():
    """It holds the superadmin notion now, so it no longer waits for a brick."""
    from apowerb.admin.guard import is_superadmin

    registry.register_supervision_scope(is_superadmin)
    assert registry.supervision_scope() is is_superadmin


def test_the_supervision_screen_did_not_come_back_with_it():
    """The panel is open source; the screen that reads other people's sessions
    is not. Moving one must not drag the other in."""
    assert client.get("/api/supervision/sessions").status_code == 404

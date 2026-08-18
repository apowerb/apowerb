"""Supervision left the core on 18/08/2026.

The screen reads *other people's* sessions, which is a sold capability, so
the open-source build must not answer on its route at all -- a flag would
have hidden the menu entry while the endpoint kept replying.

What did NOT leave is the permission question. ``may_supervise_across_accounts``
has a second caller that is core through and through:
``GET /sessions/{agent}/{user}/{session}/trace``, where reading one's OWN
trace is a core feature and supervising merely widens the guard. Moving the
predicate out would have taken that route down with it.

These tests ask the application, not its route table: this FastAPI version
expands ``include_router`` lazily, so ``app.routes`` is empty of ``/api`` at
import time and a structural assertion would pass for the wrong reason.
A 401 on a route that exists is what proves a 404 means *absent*.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock

from apowerb.core.extensions.registry import registry
from apowerb.helpers.ownership import may_supervise_across_accounts
from apowerb.main import app

client = TestClient(app, raise_server_exceptions=False)

# Exists and requires authentication: its 401 is the positive control that
# the application is wired at all, so a 404 next door is real absence.
A_ROUTE_THAT_STAYED = "/api/agents"


def test_a_core_route_still_answers():
    assert client.get(A_ROUTE_THAT_STAYED).status_code == 401


def test_the_core_answers_404_on_the_supervision_route():
    """Not hidden -- absent. Installing the brick is what brings it back."""
    assert client.get("/api/supervision/sessions").status_code == 404


def test_the_trace_route_that_needs_the_predicate_still_answers():
    """The reason the predicate stayed in the core. A 401, not a 404."""
    got = client.get("/api/adk/sessions/an-agent/a-user/a-session/trace")
    assert got.status_code == 401


@pytest.mark.asyncio
async def test_the_predicate_still_answers_no_without_a_brick():
    """The core keeps the honest default: everyone sees their own sessions."""
    registry._supervision_scope = None
    assert await may_supervise_across_accounts(AsyncMock(), object()) is False

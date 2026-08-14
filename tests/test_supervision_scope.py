"""Who may read another account's sessions.

Supervision used `is_admin`, so every administrator saw every agent on the
platform — that is how a colleague, an admin for operational reasons, read
someone else's sessions. The core cannot decide this on its own:
"superadmin" is a row in a table owned by a commercial brick, and the core
never names one. It asks, and a missing brick means no.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from apowerb.core.extensions.registry import registry
from apowerb.evaluation.run_service import may_supervise_across_accounts


@pytest.fixture(autouse=True)
def _clean_registry():
    registry.reset()
    yield
    registry.reset()


def _admin():
    u = MagicMock()
    u.role = "ADMIN"
    u.email = "admin@example.com"
    return u


@pytest.mark.asyncio
async def test_no_brick_means_nobody_crosses_accounts():
    """The core's default, and the one an open-source install runs on: you
    supervise your own sessions. An administrator is not, on his own,
    entitled to read his colleagues' conversations.
    """
    assert await may_supervise_across_accounts(AsyncMock(), _admin()) is False


@pytest.mark.asyncio
async def test_the_brick_is_asked_and_its_answer_is_used():
    registry.register_supervision_scope(AsyncMock(return_value=True))
    assert await may_supervise_across_accounts(AsyncMock(), _admin()) is True

    registry.reset()
    registry.register_supervision_scope(AsyncMock(return_value=False))
    assert await may_supervise_across_accounts(AsyncMock(), _admin()) is False


@pytest.mark.asyncio
async def test_the_brick_receives_the_session_and_the_user():
    """It has a table to read, so it needs the session, and it answers about
    a person, so it needs the person.
    """
    resolver = AsyncMock(return_value=True)
    registry.register_supervision_scope(resolver)
    db, user = AsyncMock(), _admin()

    await may_supervise_across_accounts(db, user)

    resolver.assert_awaited_once_with(db, user)


@pytest.mark.asyncio
async def test_a_truthy_answer_is_narrowed_to_a_boolean():
    """A brick returning a row, a count or None must not leak that shape
    into a security decision the caller reads as a flag.
    """
    registry.register_supervision_scope(AsyncMock(return_value=None))
    assert await may_supervise_across_accounts(AsyncMock(), _admin()) is False

    registry.reset()
    registry.register_supervision_scope(AsyncMock(return_value=(1,)))
    assert await may_supervise_across_accounts(AsyncMock(), _admin()) is True

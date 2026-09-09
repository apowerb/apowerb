"""A recorded consumption event notifies the observers.

This is the link between accounting and debiting credits. Without it, the
billing extension registers an observer that nobody ever calls -- exactly
the bug being fixed: two halves that don't talk to each other, each green
on its own side.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from apowerb.core.agent_helpers import usage_recorder as recorder


class _FakeDB:
    def add(self, _row):
        pass

    async def commit(self):
        pass


@pytest.fixture
def fake_db(monkeypatch):
    from apowerb.helpers import database

    @asynccontextmanager
    async def _session():
        yield _FakeDB()

    monkeypatch.setattr(database.sessionmanager, "session", _session, raising=False)


@pytest.fixture
def no_observer(monkeypatch):
    monkeypatch.setattr(recorder, "_post_usage_observers", [])


@pytest.mark.asyncio
async def test_observer_called_with_the_owner(fake_db, no_observer):
    seen = []

    async def observer(db, owner_id):
        seen.append(owner_id)

    recorder.register_post_usage(observer)

    await recorder._persist_usage_row(
        agent_id=1,
        agent_name="agent1",
        owner_id="client@example.com",
        model="thaink2/default",
        total_tokens=1234,
        billed_to_thaink2=True,
    )

    assert seen == ["client@example.com"]


@pytest.mark.asyncio
async def test_no_owner_means_no_observer_called(fake_db, no_observer):
    """Without an identity, there is nothing to debit -- and above all no one to debit."""
    called = False

    async def observer(db, owner_id):
        nonlocal called
        called = True

    recorder.register_post_usage(observer)

    await recorder._persist_usage_row(
        agent_id=1,
        agent_name="agent1",
        owner_id=None,
        model="thaink2/default",
        total_tokens=10,
        billed_to_thaink2=True,
    )

    assert not called


@pytest.mark.asyncio
async def test_a_failing_observer_does_not_lose_the_consumption(
    fake_db, no_observer, caplog
):
    """The usage row is already committed: a missed debit catches up on the
    next pass, it must not make the accounting disappear."""

    async def broken_observer(db, owner_id):
        raise RuntimeError("database unavailable")

    recorder.register_post_usage(broken_observer)

    with caplog.at_level("WARNING"):
        await recorder._persist_usage_row(
            agent_id=1,
            agent_name="agent1",
            owner_id="client@example.com",
            model="thaink2/default",
            total_tokens=10,
            billed_to_thaink2=True,
        )

    assert any("post-usage" in m for m in caplog.messages)

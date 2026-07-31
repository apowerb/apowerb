"""Tests for ``apowerb.core.agent_helpers.backlog_status_tool``.

The factory ``_make_get_backlog_status(agent_id)`` is what
``to_agent`` swaps in for the agent-facing
``tool_get_webhook_backlog_status`` placeholder. The closure must:

- only count rows for *its* agent_id (no cross-agent leak),
- distinguish ``in_progress`` (the row the agent is on) from
  ``pending`` / ``retrying``,
- count ``success`` / ``error`` rows from the start of the day, not
  all-time, so the operator gets a daily throughput view.

A real sqlite engine is used so the SQL itself is exercised — the
queries are simple but they are the contract with the operator.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from apowerb.scheduler import backlog_status_tool
from apowerb.helpers.database import Base
from apowerb.models import User, WebhookLog, WebhookSubscription


@pytest.fixture
async def factory(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sess = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def _session() -> AsyncIterator[AsyncSession]:
        async with sess() as s:
            yield s

    monkeypatch.setattr(
        backlog_status_tool.sessionmanager,
        "session",
        _session,
        raising=False,
    )
    yield sess
    await engine.dispose()


async def _seed(factory, *, sub_id: int = 0):
    async with factory() as db:
        if sub_id == 0:
            user = User(
                user_id=1,
                first_name="A",
                last_name="B",
                email="ops@example.com",
                password="x",
                role="USER",
            )
            db.add(user)
            await db.commit()
        sub = WebhookSubscription(
            user_id=1,
            integration_id=None,
            provider="microsoft_outlook",
            resource="me/mailFolders('Inbox')/messages",
            change_type="created",
            client_state="state",
            notification_url="https://example.com/cb",
            status="active",
            agent_id=42,
            expiration_datetime=datetime.now(timezone.utc) + timedelta(days=2),
        )
        db.add(sub)
        await db.commit()
        return sub.id


def _row(
    *,
    agent_id: int,
    status: str,
    sub_id: int,
    resource_id: str,
    subject: str = "",
    sender: str = "",
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    attempts: int = 0,
) -> WebhookLog:
    return WebhookLog(
        user_id=1,
        subscription_id=sub_id,
        agent_id=agent_id,
        trigger_event="created",
        resource_id=resource_id,
        status=status,
        email_subject=subject,
        email_sender=sender,
        started_at=started_at,
        completed_at=completed_at,
        attempts=attempts,
    )


@pytest.mark.asyncio
async def test_returns_zero_counts_when_agent_has_no_rows(factory):
    sub_id = await _seed(factory)
    tool = backlog_status_tool._make_get_backlog_status(42)

    result = await tool()

    assert result["status"] == "success"
    assert result["agent_id"] == 42
    assert result["pending_count"] == 0
    assert result["retrying_count"] == 0
    assert result["completed_today"] == 0
    assert result["failed_today"] == 0
    assert result["pending"] == []
    assert result["current"] is None


@pytest.mark.asyncio
async def test_only_counts_rows_for_the_bound_agent(factory):
    sub_id = await _seed(factory)
    async with factory() as db:
        # 2 pending for our agent, 5 pending for someone else.
        for i in range(2):
            db.add(_row(
                agent_id=42, status=WebhookLog.STATUS_PENDING,
                sub_id=sub_id, resource_id=f"msg-mine-{i}",
            ))
        for i in range(5):
            db.add(_row(
                agent_id=99, status=WebhookLog.STATUS_PENDING,
                sub_id=sub_id, resource_id=f"msg-theirs-{i}",
            ))
        await db.commit()

    result = await backlog_status_tool._make_get_backlog_status(42)()

    assert result["pending_count"] == 2
    assert all(p["id"] is not None for p in result["pending"])
    assert len(result["pending"]) == 2


@pytest.mark.asyncio
async def test_current_reflects_in_progress_row_and_subject(factory):
    sub_id = await _seed(factory)
    started = datetime.now(timezone.utc) - timedelta(seconds=30)
    async with factory() as db:
        db.add(_row(
            agent_id=42, status=WebhookLog.STATUS_IN_PROGRESS,
            sub_id=sub_id, resource_id="msg-current",
            subject="TILCO AR CF0916", sender="be@tilco.com",
            started_at=started, attempts=1,
        ))
        await db.commit()

    result = await backlog_status_tool._make_get_backlog_status(42)()

    assert result["current"] is not None
    assert result["current"]["subject"] == "TILCO AR CF0916"
    assert result["current"]["sender"] == "be@tilco.com"
    assert result["current"]["attempts"] == 1


@pytest.mark.asyncio
async def test_separates_pending_from_retrying(factory):
    sub_id = await _seed(factory)
    async with factory() as db:
        db.add(_row(
            agent_id=42, status=WebhookLog.STATUS_PENDING,
            sub_id=sub_id, resource_id="p1",
        ))
        db.add(_row(
            agent_id=42, status=WebhookLog.STATUS_PENDING,
            sub_id=sub_id, resource_id="p2",
        ))
        db.add(_row(
            agent_id=42, status=WebhookLog.STATUS_RETRYING,
            sub_id=sub_id, resource_id="r1",
        ))
        await db.commit()

    result = await backlog_status_tool._make_get_backlog_status(42)()

    assert result["pending_count"] == 2
    assert result["retrying_count"] == 1
    # Preview list mixes both kinds (FIFO across pending+retrying).
    assert len(result["pending"]) == 3


@pytest.mark.asyncio
async def test_counts_completed_today_but_not_yesterday(factory):
    sub_id = await _seed(factory)
    today = datetime.now(timezone.utc)
    yesterday = today - timedelta(days=1, hours=2)
    async with factory() as db:
        db.add(_row(
            agent_id=42, status=WebhookLog.STATUS_SUCCESS,
            sub_id=sub_id, resource_id="ok-today",
            completed_at=today,
        ))
        db.add(_row(
            agent_id=42, status=WebhookLog.STATUS_SUCCESS,
            sub_id=sub_id, resource_id="ok-yesterday",
            completed_at=yesterday,
        ))
        db.add(_row(
            agent_id=42, status=WebhookLog.STATUS_ERROR,
            sub_id=sub_id, resource_id="ko-today",
            completed_at=today,
        ))
        await db.commit()

    result = await backlog_status_tool._make_get_backlog_status(42)()

    assert result["completed_today"] == 1
    assert result["failed_today"] == 1


@pytest.mark.asyncio
async def test_pending_preview_caps_at_limit(factory, monkeypatch):
    monkeypatch.setattr(backlog_status_tool, "_PENDING_PREVIEW_LIMIT", 3)
    sub_id = await _seed(factory)
    async with factory() as db:
        for i in range(8):
            db.add(_row(
                agent_id=42, status=WebhookLog.STATUS_PENDING,
                sub_id=sub_id, resource_id=f"flood-{i}",
            ))
        await db.commit()

    result = await backlog_status_tool._make_get_backlog_status(42)()

    assert result["pending_count"] == 8
    assert len(result["pending"]) == 3


@pytest.mark.asyncio
async def test_db_failure_returns_status_error_not_exception(factory):
    """A flaky DB must not crash the agent run — it should surface as
    a soft error string the agent can mention and move on from."""
    tool = backlog_status_tool._make_get_backlog_status(42)
    # Replace sessionmanager.session with one that always raises so we
    # exercise the except branch.

    @asynccontextmanager
    async def _broken():
        raise RuntimeError("db down")
        yield None  # pragma: no cover

    backlog_status_tool.sessionmanager.session = _broken  # type: ignore[assignment]

    result = await tool()

    assert result["status"] == "error"
    assert "Backlog query failed" in result["message"]

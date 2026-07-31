"""Tests for ``apowerb.scheduler.backlog_worker``.

The worker drains the ``webhook_logs`` queue: it picks the oldest
``pending``/``retrying`` row whose ``next_attempt_at`` has elapsed,
runs the injected processor, and records the outcome. These tests
cover the full lifecycle (claim, success, retry on rate limit, retry
on generic error, max-attempts giveup, stale reclaim, dedup).

A real SQLite in-memory engine is wired in place of the production
sessionmanager so the conditional UPDATEs and unique constraints are
actually exercised — mocking those would let the worker drift from
the schema invariants we rely on for at-least-once semantics.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone


def _seconds_between(later: datetime, earlier: datetime) -> float:
    """SQLite drops tzinfo, so normalise to naive UTC before subtracting."""

    def _as_naive_utc(dt: datetime) -> datetime:
        if dt.tzinfo is None:
            return dt
        return dt.astimezone(timezone.utc).replace(tzinfo=None)

    return (_as_naive_utc(later) - _as_naive_utc(earlier)).total_seconds()
from typing import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from apowerb.helpers.database import Base
from apowerb.models import User, WebhookLog, WebhookSubscription
from apowerb.scheduler import backlog_worker


@pytest.fixture
async def sqlite_session_factory(monkeypatch):
    """Build a sqlite:///:memory: async engine and make sessionmanager
    use it for the duration of the test."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def _session() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    # Worker imports sessionmanager at module load. Patch the symbol
    # on the worker module (and on the outlook handler module, in case
    # the integration test imports it later).
    monkeypatch.setattr(
        backlog_worker.sessionmanager, "session", _session, raising=False
    )

    yield factory
    await engine.dispose()


async def _seed_user_and_subscription(factory) -> tuple[int, int]:
    async with factory() as db:
        user = User(
            user_id=1,
            first_name="Op",
            last_name="Erator",
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
        return 1, sub.id


async def _enqueue_log(
    factory,
    *,
    sub_id: int,
    user_id: int,
    resource_id: str,
    status: str = WebhookLog.STATUS_PENDING,
    next_attempt_at: datetime | None = None,
    started_at: datetime | None = None,
) -> int:
    async with factory() as db:
        log = WebhookLog(
            user_id=user_id,
            subscription_id=sub_id,
            agent_id=42,
            trigger_event="created",
            resource_id=resource_id,
            payload_json=json.dumps({"resource": resource_id}),
            status=status,
            next_attempt_at=next_attempt_at,
            started_at=started_at,
        )
        db.add(log)
        await db.commit()
        return log.id


# ---------------------------------------------------------------------------
# Pick + success path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_marks_in_progress_and_increments_attempts(
    sqlite_session_factory,
):
    user_id, sub_id = await _seed_user_and_subscription(sqlite_session_factory)
    log_id = await _enqueue_log(
        sqlite_session_factory, sub_id=sub_id, user_id=user_id, resource_id="msg-1",
    )

    claimed = await backlog_worker._claim_one()

    assert claimed is not None
    claimed_id, attempts = claimed  # _claim_one returns scalars (id, attempts)
    assert claimed_id == log_id
    assert attempts == 1
    async with sqlite_session_factory() as db:
        r = await db.get(WebhookLog, log_id)
        assert r.status == WebhookLog.STATUS_IN_PROGRESS
        assert r.started_at is not None


@pytest.mark.asyncio
async def test_claim_returns_none_when_queue_empty(sqlite_session_factory):
    await _seed_user_and_subscription(sqlite_session_factory)

    row = await backlog_worker._claim_one()

    assert row is None


@pytest.mark.asyncio
async def test_claim_skips_rows_whose_next_attempt_is_in_the_future(
    sqlite_session_factory,
):
    user_id, sub_id = await _seed_user_and_subscription(sqlite_session_factory)
    future = datetime.now(timezone.utc) + timedelta(seconds=120)
    await _enqueue_log(
        sqlite_session_factory,
        sub_id=sub_id,
        user_id=user_id,
        resource_id="msg-future",
        status=WebhookLog.STATUS_RETRYING,
        next_attempt_at=future,
    )

    row = await backlog_worker._claim_one()

    assert row is None


@pytest.mark.asyncio
async def test_claim_picks_retrying_row_when_next_attempt_has_elapsed(
    sqlite_session_factory,
):
    user_id, sub_id = await _seed_user_and_subscription(sqlite_session_factory)
    past = datetime.now(timezone.utc) - timedelta(seconds=5)
    log_id = await _enqueue_log(
        sqlite_session_factory,
        sub_id=sub_id,
        user_id=user_id,
        resource_id="msg-retry",
        status=WebhookLog.STATUS_RETRYING,
        next_attempt_at=past,
    )

    claimed = await backlog_worker._claim_one()

    assert claimed is not None
    assert claimed[0] == log_id  # (id, attempts)
    async with sqlite_session_factory() as db:
        r = await db.get(WebhookLog, log_id)
        assert r.status == WebhookLog.STATUS_IN_PROGRESS


@pytest.mark.asyncio
async def test_process_once_marks_success_on_processor_completion(
    sqlite_session_factory,
):
    user_id, sub_id = await _seed_user_and_subscription(sqlite_session_factory)
    log_id = await _enqueue_log(
        sqlite_session_factory, sub_id=sub_id, user_id=user_id, resource_id="msg-ok",
    )
    seen: list[int] = []

    async def processor(log_id_arg: int) -> str:
        seen.append(log_id_arg)
        return "agent answered"

    did_work = await backlog_worker.process_once(processor)
    assert did_work is True
    assert seen == [log_id]

    async with sqlite_session_factory() as db:
        row = await db.get(WebhookLog, log_id)
        assert row.status == WebhookLog.STATUS_SUCCESS
        assert row.agent_response == "agent answered"
        assert row.completed_at is not None
        assert row.duration_ms is not None


@pytest.mark.asyncio
async def test_process_once_returns_false_when_queue_empty(
    sqlite_session_factory,
):
    await _seed_user_and_subscription(sqlite_session_factory)

    async def processor(log_id_arg: int):
        raise AssertionError("processor must not be called when queue is empty")

    did_work = await backlog_worker.process_once(processor)
    assert did_work is False


# ---------------------------------------------------------------------------
# RateLimitError → retrying
# ---------------------------------------------------------------------------


class _FakeRateLimitError(Exception):
    """Looks enough like litellm.RateLimitError for the worker's
    string-based detection (we don't import litellm in tests)."""

    def __init__(self, message: str):
        super().__init__(message)


_GEMINI_429_PAYLOAD = (
    'litellm.RateLimitError: geminiException - {"error": {'
    '"code": 429, "status": "RESOURCE_EXHAUSTED", '
    '"message": "Quota exceeded ... Please retry in 47.143958509s.", '
    '"details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", '
    '"retryDelay": "47s"}]}}'
)


@pytest.mark.asyncio
async def test_rate_limit_error_marks_retrying_with_provider_delay(
    sqlite_session_factory,
):
    user_id, sub_id = await _seed_user_and_subscription(sqlite_session_factory)
    log_id = await _enqueue_log(
        sqlite_session_factory,
        sub_id=sub_id, user_id=user_id, resource_id="msg-429",
    )

    async def processor(log_id_arg: int):
        raise _FakeRateLimitError(_GEMINI_429_PAYLOAD)

    before = datetime.now(timezone.utc)
    did_work = await backlog_worker.process_once(processor)
    after = datetime.now(timezone.utc)
    assert did_work is True

    async with sqlite_session_factory() as db:
        row = await db.get(WebhookLog, log_id)
        assert row.status == WebhookLog.STATUS_RETRYING
        assert row.attempts == 1
        assert row.next_attempt_at is not None
        # Provider says 47s — worker schedules ~47s + small jitter
        delta = _seconds_between(row.next_attempt_at, before)
        assert 46 <= delta <= 60, (
            f"next_attempt_at should respect retryDelay, got delta={delta}"
        )
        assert "RESOURCE_EXHAUSTED" in (row.error_message or "")


@pytest.mark.asyncio
async def test_rate_limit_without_explicit_delay_uses_fallback(
    sqlite_session_factory,
):
    user_id, sub_id = await _seed_user_and_subscription(sqlite_session_factory)
    log_id = await _enqueue_log(
        sqlite_session_factory,
        sub_id=sub_id, user_id=user_id, resource_id="msg-429-nodelay",
    )

    async def processor(log_id_arg: int):
        raise _FakeRateLimitError(
            "RateLimitError: 429 Too Many Requests (no retry hint)"
        )

    before = datetime.now(timezone.utc)
    await backlog_worker.process_once(processor)

    async with sqlite_session_factory() as db:
        row = await db.get(WebhookLog, log_id)
        assert row.status == WebhookLog.STATUS_RETRYING
        delta = _seconds_between(row.next_attempt_at, before)
        # Fallback ~60s + jitter
        assert 59 <= delta <= 70


# ---------------------------------------------------------------------------
# Generic error → exponential backoff
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generic_error_uses_exponential_backoff(sqlite_session_factory):
    user_id, sub_id = await _seed_user_and_subscription(sqlite_session_factory)
    log_id = await _enqueue_log(
        sqlite_session_factory,
        sub_id=sub_id, user_id=user_id, resource_id="msg-boom",
    )

    async def processor(log_id_arg: int):
        raise RuntimeError("upstream gateway 502")

    before = datetime.now(timezone.utc)
    await backlog_worker.process_once(processor)

    async with sqlite_session_factory() as db:
        row = await db.get(WebhookLog, log_id)
        assert row.status == WebhookLog.STATUS_RETRYING
        # First attempt → 5s
        delta = _seconds_between(row.next_attempt_at, before)
        assert 4 <= delta <= 10, f"expected ~5s backoff, got {delta}"


@pytest.mark.asyncio
async def test_giveup_after_max_attempts_marks_error(
    sqlite_session_factory, monkeypatch,
):
    monkeypatch.setattr(backlog_worker, "_MAX_ATTEMPTS", 2)
    user_id, sub_id = await _seed_user_and_subscription(sqlite_session_factory)
    log_id = await _enqueue_log(
        sqlite_session_factory,
        sub_id=sub_id, user_id=user_id, resource_id="msg-doomed",
    )

    async def processor(log_id_arg: int):
        raise RuntimeError("nope")

    # Force next_attempt_at to be elapsed so we can re-pick immediately.
    async def _drive():
        for _ in range(3):
            await backlog_worker.process_once(processor)
            async with sqlite_session_factory() as db:
                row = await db.get(WebhookLog, log_id)
                if row.status == WebhookLog.STATUS_RETRYING:
                    row.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                    await db.commit()

    await _drive()
    async with sqlite_session_factory() as db:
        row = await db.get(WebhookLog, log_id)
        assert row.status == WebhookLog.STATUS_ERROR
        assert row.attempts >= 2
        assert row.completed_at is not None


# ---------------------------------------------------------------------------
# Stale reclaim
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reclaim_returns_stale_in_progress_to_pending(
    sqlite_session_factory,
):
    user_id, sub_id = await _seed_user_and_subscription(sqlite_session_factory)
    long_ago = datetime.now(timezone.utc) - timedelta(seconds=3600)
    log_id = await _enqueue_log(
        sqlite_session_factory,
        sub_id=sub_id,
        user_id=user_id,
        resource_id="msg-stale",
        status=WebhookLog.STATUS_IN_PROGRESS,
        started_at=long_ago,
    )

    reclaimed = await backlog_worker._reclaim_stale_in_progress()

    assert reclaimed == 1
    async with sqlite_session_factory() as db:
        row = await db.get(WebhookLog, log_id)
        assert row.status == WebhookLog.STATUS_PENDING


@pytest.mark.asyncio
async def test_reclaim_leaves_recent_in_progress_alone(
    sqlite_session_factory,
):
    user_id, sub_id = await _seed_user_and_subscription(sqlite_session_factory)
    recent = datetime.now(timezone.utc) - timedelta(seconds=30)
    log_id = await _enqueue_log(
        sqlite_session_factory,
        sub_id=sub_id,
        user_id=user_id,
        resource_id="msg-fresh",
        status=WebhookLog.STATUS_IN_PROGRESS,
        started_at=recent,
    )

    reclaimed = await backlog_worker._reclaim_stale_in_progress()

    assert reclaimed == 0
    async with sqlite_session_factory() as db:
        row = await db.get(WebhookLog, log_id)
        assert row.status == WebhookLog.STATUS_IN_PROGRESS


# ---------------------------------------------------------------------------
# Dedup constraint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unique_constraint_blocks_duplicate_resource_id(
    sqlite_session_factory,
):
    user_id, sub_id = await _seed_user_and_subscription(sqlite_session_factory)
    await _enqueue_log(
        sqlite_session_factory,
        sub_id=sub_id, user_id=user_id, resource_id="msg-dedup",
    )

    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        await _enqueue_log(
            sqlite_session_factory,
            sub_id=sub_id, user_id=user_id, resource_id="msg-dedup",
        )


# ---------------------------------------------------------------------------
# Retry-delay parser
# ---------------------------------------------------------------------------


def test_parse_retry_delay_extracts_seconds_from_retry_info():
    delay = backlog_worker._parse_retry_delay_from_error(
        Exception('"retryDelay": "47s"')
    )
    assert delay == 47.0


def test_parse_retry_delay_extracts_seconds_from_human_message():
    delay = backlog_worker._parse_retry_delay_from_error(
        Exception("Please retry in 12.5s.")
    )
    assert delay == 12.5


def test_parse_retry_delay_returns_none_when_absent():
    delay = backlog_worker._parse_retry_delay_from_error(
        Exception("totally unrelated error")
    )
    assert delay is None


# ---------------------------------------------------------------------------
# Global rate-limit circuit breaker
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_rate_limit_cooldown():
    """Every test starts with a clean cooldown so test order cannot
    leak a cooldown set by an earlier rate-limit test."""
    backlog_worker._RATE_LIMIT_COOLDOWN_UNTIL = None
    yield
    backlog_worker._RATE_LIMIT_COOLDOWN_UNTIL = None


@pytest.mark.asyncio
async def test_rate_limit_sets_global_cooldown(sqlite_session_factory):
    """After a rate-limit failure, the next pick must be deferred by
    the provider's retry window. Per-row backoff is not enough — the
    next row will hit the same quota minute."""
    user_id, sub_id = await _seed_user_and_subscription(sqlite_session_factory)
    await _enqueue_log(
        sqlite_session_factory,
        sub_id=sub_id, user_id=user_id, resource_id="msg-rl",
    )

    async def processor(log_id_arg):
        raise _FakeRateLimitError(_GEMINI_429_PAYLOAD)

    before = datetime.now(timezone.utc)
    await backlog_worker.process_once(processor)

    # Cooldown set to ~ retry_delay seconds in the future.
    cooldown = backlog_worker._RATE_LIMIT_COOLDOWN_UNTIL
    assert cooldown is not None
    delta = _seconds_between(cooldown, before)
    assert 46 <= delta <= 60, (
        f"cooldown should match retry_delay (~47s), got {delta}"
    )


@pytest.mark.asyncio
async def test_process_once_skips_during_cooldown(sqlite_session_factory):
    """While the cooldown is active, ``process_once`` must return False
    and leave the queue untouched — picking another row would re-hit
    the same quota window and turn it into another wasted retry."""
    user_id, sub_id = await _seed_user_and_subscription(sqlite_session_factory)
    log_id = await _enqueue_log(
        sqlite_session_factory,
        sub_id=sub_id, user_id=user_id, resource_id="msg-during-cooldown",
    )

    backlog_worker._RATE_LIMIT_COOLDOWN_UNTIL = (
        datetime.now(timezone.utc) + timedelta(seconds=30)
    )

    called = []

    async def processor(log_id_arg):
        called.append(log_id_arg)
        return "ok"

    did_work = await backlog_worker.process_once(processor)

    assert did_work is False
    assert called == [], "processor must not be invoked during cooldown"
    # Row still pending — the cooldown didn't claim it.
    async with sqlite_session_factory() as db:
        row = await db.get(WebhookLog, log_id)
        assert row.status == WebhookLog.STATUS_PENDING


@pytest.mark.asyncio
async def test_process_once_resumes_after_cooldown_window(sqlite_session_factory):
    """Once the cooldown timestamp has elapsed, picking resumes
    normally without any extra reset — the timestamp comparison alone
    gates the gate."""
    user_id, sub_id = await _seed_user_and_subscription(sqlite_session_factory)
    await _enqueue_log(
        sqlite_session_factory,
        sub_id=sub_id, user_id=user_id, resource_id="msg-after-cooldown",
    )

    # Cooldown already elapsed.
    backlog_worker._RATE_LIMIT_COOLDOWN_UNTIL = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    )

    called = []

    async def processor(log_id_arg):
        called.append(log_id_arg)
        return "post-cooldown ok"

    did_work = await backlog_worker.process_once(processor)

    assert did_work is True
    assert len(called) == 1, "processor must run once the cooldown has passed"


# ---------------------------------------------------------------------------
# start_in_background — visibility for SCEI-prod-style "did the worker
# actually start?" forensics. Without these logs, a silent
# asyncio.create_task crash leaves journalctl empty and the queue
# stalls without any breadcrumb.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_in_background_logs_spawn(caplog):
    """The startup log must mention the processor name so the operator
    can grep journalctl after a deploy and confirm the right callback
    was wired."""
    backlog_worker._WORKER_TASK = None  # reset singleton for the test

    async def my_processor(log_id_arg):  # noqa: ARG001 — signature must match
        return None

    caplog.set_level("INFO", logger="apowerb.scheduler.backlog_worker")
    task = backlog_worker.start_in_background(my_processor)
    try:
        assert task is not None
        spawn_msgs = [
            r.getMessage() for r in caplog.records
            if "spawning worker task" in r.getMessage()
        ]
        assert spawn_msgs, "start_in_background must log the spawn at INFO"
        assert "my_processor" in spawn_msgs[0], (
            "spawn log must name the processor so the operator can confirm"
            " the right callback was wired"
        )
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        backlog_worker._WORKER_TASK = None


@pytest.mark.asyncio
async def test_start_in_background_logs_silent_crash(caplog, monkeypatch):
    """If the worker coroutine raises before its main loop, the task
    finishes silently and the queue stalls. The done-callback must log
    that crash at ERROR so an operator sees something in journalctl."""
    backlog_worker._WORKER_TASK = None

    async def crashing_run_worker(processor, poll_interval_seconds=2.0):
        raise RuntimeError("simulated early crash")

    # Stub the schema migration so we don't try to connect to the
    # configured (postgres) DB just to get to the run_worker call.
    async def _noop_schema():
        return True

    monkeypatch.setattr(backlog_worker, "run_worker", crashing_run_worker)
    from apowerb.scheduler import backlog_migrations
    monkeypatch.setattr(
        backlog_migrations, "ensure_webhook_logs_schema", _noop_schema
    )

    caplog.set_level("ERROR", logger="apowerb.scheduler.backlog_worker")

    async def my_processor(log_id_arg):  # noqa: ARG001
        return None

    task = backlog_worker.start_in_background(my_processor)
    # Give the event loop a few ticks so the crash + done-callback fire.
    for _ in range(5):
        await asyncio.sleep(0)
    if not task.done():
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except (RuntimeError, asyncio.TimeoutError):
            pass

    crash_logs = [
        r for r in caplog.records
        if "worker task crashed unexpectedly" in r.getMessage()
    ]
    assert crash_logs, (
        "done-callback must log silent crashes at ERROR — without "
        "this the queue stalls invisibly"
    )
    backlog_worker._WORKER_TASK = None


# ---------------------------------------------------------------------------
# Schema auto-migration — runs once at worker boot. Idempotent so a
# second worker instance (or a process restart) never collides with
# itself; safe enough that an unexpected DDL failure logs but does not
# crash the loop.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_schema_skips_on_sqlite(sqlite_session_factory, caplog):
    """The runtime fixture builds the full schema via
    ``Base.metadata.create_all``, so the auto-migration has nothing
    to do on sqlite. The function must detect the dialect and skip
    cleanly — the alternative is a hard failure on every test run
    because sqlite does not support ``ADD COLUMN IF NOT EXISTS``.
    """
    from apowerb.scheduler import backlog_migrations

    caplog.set_level("INFO", logger="apowerb.scheduler.backlog_migrations")
    result = await backlog_migrations.ensure_webhook_logs_schema()

    assert result is True
    skip_logs = [
        r.getMessage() for r in caplog.records
        if "schema migration skipped on sqlite" in r.getMessage()
    ]
    assert skip_logs, "sqlite path must announce the skip in INFO"


@pytest.mark.asyncio
async def test_ensure_schema_returns_false_on_failure(monkeypatch, caplog):
    """A bad DDL run (network, permissions, malformed schema) must
    surface as a soft failure — the worker keeps draining whatever
    schema is in place and the operator falls back to the standalone
    script."""
    from apowerb.scheduler import backlog_migrations

    class _ExplodingSession:
        async def __aenter__(self):
            raise RuntimeError("simulated DDL failure")

        async def __aexit__(self, *exc):
            return False

    def _broken_session():
        return _ExplodingSession()

    monkeypatch.setattr(
        backlog_migrations.sessionmanager, "session", _broken_session
    )
    caplog.set_level("ERROR", logger="apowerb.scheduler.backlog_migrations")

    result = await backlog_migrations.ensure_webhook_logs_schema()

    assert result is False
    err_logs = [
        r for r in caplog.records
        if "schema migration failed" in r.getMessage()
    ]
    assert err_logs, "DDL failures must log at ERROR with recovery hint"


@pytest.mark.asyncio
async def test_start_in_background_is_idempotent(caplog):
    """Calling twice in the same process must reuse the existing task
    (defends against FastAPI on_event firing the startup hook twice)."""
    backlog_worker._WORKER_TASK = None

    async def my_processor(log_id_arg):  # noqa: ARG001
        return None

    caplog.set_level("INFO", logger="apowerb.scheduler.backlog_worker")
    first = backlog_worker.start_in_background(my_processor)
    second = backlog_worker.start_in_background(my_processor)
    try:
        assert first is second
        already_msgs = [
            r.getMessage() for r in caplog.records
            if "already running" in r.getMessage()
        ]
        assert already_msgs, "second call must log the no-op"
    finally:
        first.cancel()
        try:
            await first
        except asyncio.CancelledError:
            pass
        backlog_worker._WORKER_TASK = None


@pytest.mark.asyncio
async def test_success_after_retry_clears_stale_error_message(
    sqlite_session_factory,
):
    """A row that failed once keeps its error_message while retrying; once a
    retry SUCCEEDS the stale error must be cleared — otherwise the dashboard
    shows status=success with the old failure text side by side."""
    user_id, sub_id = await _seed_user_and_subscription(sqlite_session_factory)
    log_id = await _enqueue_log(
        sqlite_session_factory,
        sub_id=sub_id, user_id=user_id, resource_id="msg-retry-ok",
    )
    async with sqlite_session_factory() as db:
        row = await db.get(WebhookLog, log_id)
        row.error_message = "ClientError: Failed to run ADK agent: 500"
        await db.commit()

    async def processor(log_id_arg: int):
        return "ok"

    did_work = await backlog_worker.process_once(processor)
    assert did_work is True

    async with sqlite_session_factory() as db:
        row = await db.get(WebhookLog, log_id)
        assert row.status == WebhookLog.STATUS_SUCCESS
        assert row.error_message is None, (
            "a successful retry must clear the stale error_message"
        )

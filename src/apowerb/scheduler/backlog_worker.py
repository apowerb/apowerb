"""Backlog worker — drains the webhook_logs queue.

Sits behind every webhook ingress so that the LLM run is decoupled from
the provider notification. One in-process asyncio task per backend
instance picks rows in FIFO order, runs the agent for the row, and
records the outcome. ``RateLimitError`` is treated as recoverable:
the row is requeued with ``next_attempt_at = now + retry_delay``
(extracted from the provider payload when available) so the quota
window has time to roll over.

Why a queue instead of FastAPI BackgroundTasks:
- Microsoft Graph re-delivers notifications when our endpoint returns
  late or 5xx. Without a queue, every retry spawned a fresh agent run
  and the rate-limited Gemini quota was hit again on each retry. With
  the queue, the row sits as ``pending`` and the retry just bumps
  ``attempts``.
- Multiple notifications arriving in the same minute would each spawn a
  parallel agent run and saturate the Gemini per-minute input-token
  quota. The queue serialises agent execution to one row at a time.
"""

from __future__ import annotations

import asyncio
from apowerb.configs.th2logger import setup_logging
import re
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional

from sqlalchemy import select, update

from apowerb.helpers.database import sessionmanager
from apowerb.models import WebhookLog
from apowerb.helpers import notify_etl

logger = setup_logging(__name__)


_BACKOFF_BASE_SECONDS = (5, 30, 120, 600)
_STALE_IN_PROGRESS_SECONDS = 600
_MAX_ATTEMPTS = 8

_WORKER_TASK: Optional[asyncio.Task] = None

# Global rate-limit circuit breaker. When the underlying LLM provider
# returns a quota error, we don't just reschedule the *current* row —
# we pause every subsequent pick until the provider's retry window
# has elapsed. Otherwise the worker keeps draining the queue at full
# speed (one pick every ~2 s), each pick paying the full input-token
# cost of an LLM call, which re-hits the per-minute quota immediately
# and turns every row into a doomed retry. SCEI prod 2026-05-07 hit
# this loop after PR #137: 100+ pending rows, every one of them
# RateLimit'd within seconds because the cooldown was per-row instead
# of process-wide.
_RATE_LIMIT_COOLDOWN_UNTIL: Optional[datetime] = None


def _parse_retry_delay_from_error(exc: BaseException) -> Optional[float]:
    """Pull the retry delay (seconds) out of a provider 429 payload.

    Google Gemini surfaces a ``RetryInfo`` block in its 429 JSON body
    (``"retryDelay": "47s"``) and a human-readable ``Please retry in
    47.143958509s.`` line. Parsing either lets the worker wait exactly
    as long as the provider asks instead of guessing.
    """
    msg = str(exc)
    m = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', msg)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    m = re.search(r"retry in (\d+(?:\.\d+)?)s", msg)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def _is_rate_limit_error(exc: BaseException) -> bool:
    name = type(exc).__name__
    if name in {"RateLimitError", "ResourceExhausted"}:
        return True
    msg = str(exc)
    if "RateLimitError" in msg or "RESOURCE_EXHAUSTED" in msg:
        return True
    return "429" in msg and "Too Many Requests" in msg


async def _reclaim_stale_in_progress(
    now: Optional[datetime] = None,
    stale_seconds: int = _STALE_IN_PROGRESS_SECONDS,
) -> int:
    """Reset rows stuck in_progress past the cutoff back to pending.

    Called once per tick — keeps the queue alive when a worker process
    crashes mid-run.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=stale_seconds)
    async with sessionmanager.session() as db:
        stmt = (
            update(WebhookLog)
            .where(
                WebhookLog.status == WebhookLog.STATUS_IN_PROGRESS,
                WebhookLog.started_at.is_not(None),
                WebhookLog.started_at < cutoff,
            )
            .values(status=WebhookLog.STATUS_PENDING)
        )
        result = await db.execute(stmt)
        await db.commit()
        return result.rowcount or 0


async def _claim_one(now: Optional[datetime] = None) -> Optional[tuple[int, int]]:
    """Atomically pick the oldest ready row and mark it in_progress.

    Uses a conditional UPDATE so two workers racing on the same row can
    never both succeed — the second sees zero rows updated and moves
    on. Returns ``(log_id, attempts)`` as SCALARS after the claim, or
    ``None`` when the queue has nothing ready.

    Returns scalars, NEVER an ORM instance: a detached ``WebhookLog``
    crossing the session boundary raises ``DetachedInstanceError`` on any
    downstream lazy-refresh (incident 2026-05-29 — webhooks looped in the
    backlog). The processor reloads its own row, in its own session, from
    the id.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    async with sessionmanager.session() as db:
        candidate_id = (
            await db.execute(
                select(WebhookLog.id)
                .where(
                    WebhookLog.status.in_(
                        (WebhookLog.STATUS_PENDING, WebhookLog.STATUS_RETRYING)
                    ),
                    (
                        WebhookLog.next_attempt_at.is_(None)
                        | (WebhookLog.next_attempt_at <= now)
                    ),
                )
                .order_by(WebhookLog.id.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if candidate_id is None:
            return None

        result = await db.execute(
            update(WebhookLog)
            .where(
                WebhookLog.id == candidate_id,
                WebhookLog.status.in_(
                    (WebhookLog.STATUS_PENDING, WebhookLog.STATUS_RETRYING)
                ),
            )
            .values(
                status=WebhookLog.STATUS_IN_PROGRESS,
                attempts=WebhookLog.attempts + 1,
                started_at=now,
                next_attempt_at=None,
            )
            .returning(WebhookLog.attempts)
        )
        claimed_attempts = result.scalar_one_or_none()
        await db.commit()
        if claimed_attempts is None:
            # Lost the race (another worker claimed it first).
            return None
        return (candidate_id, int(claimed_attempts))


async def _mark_success(
    log_id: int,
    agent_response: Optional[str],
    duration_ms: Optional[int],
) -> None:
    async with sessionmanager.session() as db:
        row = await db.get(WebhookLog, log_id)
        if row is None:
            return
        row.status = WebhookLog.STATUS_SUCCESS
        row.agent_response = agent_response
        row.duration_ms = duration_ms
        row.completed_at = datetime.now(timezone.utc)
        row.force_reprocess = False
        # Clear the previous attempt's error: a retried row would otherwise
        # keep a stale error_message next to status=success, which the
        # dashboard renders as a failure (operator confusion, 2026-07-16).
        row.error_message = None
        await db.commit()


async def _mark_retrying(
    log_id: int,
    error_message: str,
    next_attempt_at: datetime,
) -> None:
    async with sessionmanager.session() as db:
        row = await db.get(WebhookLog, log_id)
        if row is None:
            return
        row.status = WebhookLog.STATUS_RETRYING
        row.error_message = (error_message or "")[:4000]
        row.next_attempt_at = next_attempt_at
        await db.commit()


async def _mark_error(log_id: int, error_message: str) -> None:
    async with sessionmanager.session() as db:
        row = await db.get(WebhookLog, log_id)
        if row is None:
            return
        row.status = WebhookLog.STATUS_ERROR
        row.error_message = (error_message or "")[:4000]
        row.completed_at = datetime.now(timezone.utc)
        row.force_reprocess = False
        await db.commit()


# Receives the webhook log_id (scalar) — NOT an ORM instance. The processor
# loads its own WebhookLog in its own session (see _claim_one for why).
ProcessorFn = Callable[[int], Awaitable[Optional[str]]]


def _backoff_for_attempt(attempt: int) -> float:
    idx = min(max(attempt - 1, 0), len(_BACKOFF_BASE_SECONDS) - 1)
    return float(_BACKOFF_BASE_SECONDS[idx])


async def process_once(processor: ProcessorFn) -> bool:
    """Drain one row from the queue.

    Returns True when a row was processed (success or recoverable
    failure), False when nothing was ready (including the case where
    the global rate-limit circuit breaker is currently holding off
    further picks). Tests drive this directly so the infinite loop
    stays out of the unit-test path.
    """
    global _RATE_LIMIT_COOLDOWN_UNTIL
    await _reclaim_stale_in_progress()

    # Honour the global rate-limit cooldown: when the most recent LLM
    # call returned 429, every other row in the queue is going to hit
    # the same quota window if we pick it now. Sleep through the
    # provider's retry hint instead of churning failed retries.
    now = datetime.now(timezone.utc)
    if _RATE_LIMIT_COOLDOWN_UNTIL is not None and now < _RATE_LIMIT_COOLDOWN_UNTIL:
        return False

    claimed = await _claim_one()
    if claimed is None:
        return False

    log_id, attempts = claimed
    started = datetime.now(timezone.utc)
    try:
        agent_response = await processor(log_id)
        duration_ms = int(
            (datetime.now(timezone.utc) - started).total_seconds() * 1000
        )
        await _mark_success(log_id, agent_response, duration_ms)
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        logger.warning("[BACKLOG] log_id=%s failed: %s", log_id, message)
        if attempts >= _MAX_ATTEMPTS:
            await _mark_error(log_id, message)
            return True

        if _is_rate_limit_error(exc):
            delay = _parse_retry_delay_from_error(exc)
            if delay is None or delay <= 0:
                delay = 60.0
            delay += min(5.0, max(0.5, delay * 0.05))
            # Trip the global circuit breaker so subsequent picks wait
            # for the same window. Without this we keep claiming rows
            # at the poll interval (~2 s) and pay the full LLM input
            # cost on each one — a single saturated minute turns into
            # 30+ retried failures.
            _RATE_LIMIT_COOLDOWN_UNTIL = (
                datetime.now(timezone.utc) + timedelta(seconds=delay)
            )
            logger.info(
                "[BACKLOG] rate-limit cooldown set for %.1fs (until %s)",
                delay, _RATE_LIMIT_COOLDOWN_UNTIL.isoformat(),
            )
        else:
            delay = _backoff_for_attempt(attempts)

        next_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
        await _mark_retrying(log_id, message, next_at)
    return True


async def run_worker(
    processor: ProcessorFn,
    poll_interval_seconds: float = 2.0,
) -> None:
    """Forever loop. Stops only on cancellation."""
    logger.info("[BACKLOG] worker started")
    try:
        while True:
            try:
                did_work = await process_once(processor)
            except Exception as exc:
                logger.exception("[BACKLOG] tick raised, sleeping before retry")
                await notify_etl.notify_job_failure(
                    "backlog-worker-tick", f"{type(exc).__name__}: {exc}"
                )
                did_work = False
            if not did_work:
                await asyncio.sleep(poll_interval_seconds)
    except asyncio.CancelledError:
        logger.info("[BACKLOG] worker cancelled")
        raise


def start_in_background(processor: ProcessorFn) -> asyncio.Task:
    """Idempotent — spawn the worker once per process.

    Logs the spawn at INFO so an operator can confirm the FastAPI
    startup hook actually reached this point (cf. SCEI prod 2026-05-07
    where the worker silently never started and there was no log to
    diagnose). Attaches a done-callback that surfaces unexpected
    crashes — without it, an exception inside ``run_worker`` would
    finish the task silently and the queue would just stop draining
    with nothing in journalctl.
    """
    global _WORKER_TASK
    if _WORKER_TASK is not None and not _WORKER_TASK.done():
        logger.info(
            "[BACKLOG] start_in_background: worker already running (task=%r)",
            _WORKER_TASK,
        )
        return _WORKER_TASK

    logger.info(
        "[BACKLOG] start_in_background: spawning worker task "
        "(processor=%s)",
        getattr(processor, "__name__", repr(processor)),
    )

    async def _bootstrap_and_run() -> None:
        # Self-heal the webhook_logs schema on first boot of any
        # environment that pre-dates PR #120 (cf. SCEI prod 2026-05-07
        # where the table was missing the queue columns and every
        # webhook crashed). Idempotent — no-op once the columns are
        # there. Failures are logged but never block the loop.
        from apowerb.scheduler.backlog_migrations import (
            ensure_webhook_logs_schema,
        )
        await ensure_webhook_logs_schema()
        await run_worker(processor)

    _WORKER_TASK = asyncio.create_task(_bootstrap_and_run())

    def _on_worker_done(task: asyncio.Task) -> None:
        if task.cancelled():
            logger.info("[BACKLOG] worker task cancelled (clean shutdown)")
            return
        exc = task.exception()
        if exc is not None:
            logger.error(
                "[BACKLOG] worker task crashed unexpectedly — queue will "
                "stop draining until the service restarts. exception=%r",
                exc,
                exc_info=exc,
            )
        else:
            # ``run_worker`` is supposed to loop forever. Reaching this
            # branch means the loop exited without raising, which is
            # nominal only on ``asyncio.CancelledError`` — already
            # handled above.
            logger.warning(
                "[BACKLOG] worker task exited cleanly without cancel — "
                "this should not happen, investigate.",
            )

    _WORKER_TASK.add_done_callback(_on_worker_done)
    return _WORKER_TASK

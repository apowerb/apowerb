"""Tests for ETL/job failure alerts (throttle + routing to super admin)."""

from unittest.mock import AsyncMock

import pytest

from th2agent.helpers import notify_etl


@pytest.fixture(autouse=True)
def _clear_throttle():
    notify_etl._last_sent.clear()
    yield
    notify_etl._last_sent.clear()


@pytest.mark.asyncio
async def test_sends_to_super_admin(monkeypatch):
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr(notify_etl.system_mailer, "send_system_email", sent)
    ok = await notify_etl.notify_job_failure("agents-pipeline", "boom", now=1000.0)
    assert ok is True
    sent.assert_awaited_once()
    kwargs = sent.call_args.kwargs
    assert kwargs["to"] == [notify_etl.get_settings().super_admin_email, "support@thaink2.com", "david.gnaglo@thaink2.com"]
    assert kwargs["subject"] == "[ETL][FAILED] agents-pipeline"
    assert "boom" in kwargs["html"]


@pytest.mark.asyncio
async def test_throttles_duplicate_within_window(monkeypatch):
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr(notify_etl.system_mailer, "send_system_email", sent)
    assert await notify_etl.notify_job_failure("job", "err", now=1000.0) is True
    # Same (job, error) 5 min later -> throttled, no second send.
    assert await notify_etl.notify_job_failure("job", "err", now=1000.0 + 300) is False
    assert sent.await_count == 1


@pytest.mark.asyncio
async def test_resends_after_window(monkeypatch):
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr(notify_etl.system_mailer, "send_system_email", sent)
    assert await notify_etl.notify_job_failure("job", "err", now=1000.0) is True
    assert await notify_etl.notify_job_failure("job", "err", now=1000.0 + 901) is True
    assert sent.await_count == 2


@pytest.mark.asyncio
async def test_distinct_jobs_not_throttled(monkeypatch):
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr(notify_etl.system_mailer, "send_system_email", sent)
    await notify_etl.notify_job_failure("job-a", "err", now=1000.0)
    await notify_etl.notify_job_failure("job-b", "err", now=1000.0)
    assert sent.await_count == 2


@pytest.mark.asyncio
async def test_never_raises_on_send_error(monkeypatch):
    boom = AsyncMock(side_effect=RuntimeError("graph down"))
    monkeypatch.setattr(notify_etl.system_mailer, "send_system_email", boom)
    # Must swallow — alerting cannot mask the original job failure.
    assert await notify_etl.notify_job_failure("job", "err", now=1000.0) is False

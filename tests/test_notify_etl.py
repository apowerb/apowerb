"""Tests for ETL/job failure alerts (throttle + routing to super admin)."""

from unittest.mock import AsyncMock

import pytest

from apowerb.helpers import notify_etl


@pytest.fixture(autouse=True)
def _configured_recipients(monkeypatch):
    """The recipients are a deployment identity, with no default any more."""
    settings = notify_etl.get_settings()
    monkeypatch.setattr(settings, "super_admin_email", "admin@example.com")
    monkeypatch.setattr(settings, "etl_alert_recipients", "support@example.com")


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
    assert kwargs["to"] == ["admin@example.com", "support@example.com"]
    assert kwargs["subject"] == "[ETL][FAILED] agents-pipeline"
    assert "boom" in kwargs["html"]


@pytest.mark.asyncio
async def test_no_recipient_configured_sends_nothing(monkeypatch):
    """With no recipient, the alert is dropped instead of failing to send."""
    settings = notify_etl.get_settings()
    monkeypatch.setattr(settings, "super_admin_email", "")
    monkeypatch.setattr(settings, "etl_alert_recipients", "")
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr(notify_etl.system_mailer, "send_system_email", sent)
    assert await notify_etl.notify_job_failure("job", "err", now=1000.0) is False
    sent.assert_not_awaited()


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


# -- le lien profond vers Supervision --------------------------------------
# Supervision est partie en brique le 18/08. L'alerte ETL proposait « Voir
# dans la supervision » avec une URL construite dans le noyau : sans la
# brique, ce bouton envoie par mail vers une page qui n'existe pas.


@pytest.mark.asyncio
async def test_no_brick_means_no_button_rather_than_a_dead_one(monkeypatch):
    """An alert that still tells you what failed beats one with a 404 in it."""
    from apowerb.core.extensions.registry import registry

    monkeypatch.setattr(registry, "_supervision_link", None, raising=False)
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr(notify_etl.system_mailer, "send_system_email", sent)

    await notify_etl.notify_job_failure("agents-pipeline", "boom", now=2000.0)

    html = sent.call_args.kwargs["html"]
    assert "/supervision" not in html
    assert "boom" in html  # l'alerte reste une alerte


@pytest.mark.asyncio
async def test_the_brick_decides_where_the_button_points(monkeypatch):
    """The core holds the reference and the public URL; the path is the
    brick's, because the screen is the brick's."""
    from apowerb.core.extensions.registry import registry

    monkeypatch.setattr(
        registry,
        "_supervision_link",
        lambda base_url, search: f"{base_url}/elsewhere?q={search}",
        raising=False,
    )
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr(notify_etl.system_mailer, "send_system_email", sent)

    await notify_etl.notify_job_failure("job", "boom", context="run-42", now=3000.0)

    html = sent.call_args.kwargs["html"]
    assert "/elsewhere?q=run-42" in html

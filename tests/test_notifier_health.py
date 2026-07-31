"""Tests for notifier owner-integration supervision."""

import contextlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from apowerb.helpers import notifier_health
from apowerb.scheduler import notifier_watch


class _Result:
    def __init__(self, first=None, scalar=None):
        self._first = first
        self._scalar = scalar

    def first(self):
        return self._first

    def scalar_one_or_none(self):
        return self._scalar


class _FakeSession:
    """Returns scripted results in call order."""

    def __init__(self, results):
        self._results = list(results)
        self._i = 0

    async def execute(self, stmt):
        r = self._results[self._i]
        self._i += 1
        return r


def _patch_session(monkeypatch, session):
    @contextlib.asynccontextmanager
    async def _cm():
        yield session

    monkeypatch.setattr(notifier_health.sessionmanager, "session", lambda: _cm())


def _integration(refresh_token="rt"):
    ig = MagicMock()
    ig.refresh_token = refresh_token
    return ig


@pytest.mark.asyncio
async def test_owner_not_found_unhealthy(monkeypatch):
    _patch_session(monkeypatch, _FakeSession([_Result(first=None)]))
    res = await notifier_health.check_notifier_owner()
    assert res["healthy"] is False
    assert "not found" in res["detail"]


@pytest.mark.asyncio
async def test_integration_missing_unhealthy(monkeypatch):
    _patch_session(
        monkeypatch,
        _FakeSession([_Result(first=(7,)), _Result(scalar=None)]),
    )
    res = await notifier_health.check_notifier_owner()
    assert res["healthy"] is False
    assert "no microsoft_outlook integration" in res["detail"]


@pytest.mark.asyncio
async def test_no_refresh_token_unhealthy(monkeypatch):
    _patch_session(
        monkeypatch,
        _FakeSession([_Result(first=(7,)), _Result(scalar=_integration(refresh_token=None))]),
    )
    res = await notifier_health.check_notifier_owner()
    assert res["healthy"] is False
    assert "refresh_token" in res["detail"]


@pytest.mark.asyncio
async def test_present_healthy_cheap(monkeypatch):
    _patch_session(
        monkeypatch,
        _FakeSession([_Result(first=(7,)), _Result(scalar=_integration())]),
    )
    res = await notifier_health.check_notifier_owner(deep=False)
    assert res["healthy"] is True


@pytest.mark.asyncio
async def test_deep_token_revoked_unhealthy(monkeypatch):
    _patch_session(
        monkeypatch,
        _FakeSession([_Result(first=(7,)), _Result(scalar=_integration()), _Result(first=(7,))]),
    )
    monkeypatch.setattr(
        notifier_health.MicrosoftIntegrationService,
        "get_valid_access_token",
        AsyncMock(return_value=None),
    )
    res = await notifier_health.check_notifier_owner(deep=True)
    assert res["healthy"] is False
    assert "revoked" in res["detail"]


@pytest.mark.asyncio
async def test_deep_token_ok_healthy(monkeypatch):
    _patch_session(
        monkeypatch,
        _FakeSession([_Result(first=(7,)), _Result(scalar=_integration()), _Result(first=(7,))]),
    )
    monkeypatch.setattr(
        notifier_health.MicrosoftIntegrationService,
        "get_valid_access_token",
        AsyncMock(return_value="valid-token"),
    )
    res = await notifier_health.check_notifier_owner(deep=True)
    assert res["healthy"] is True


@pytest.mark.asyncio
async def test_alert_uses_independent_channel_not_shared_mailbox(monkeypatch):
    """The alert MUST go via email_sender (SMTP), never the shared mailbox."""
    sent = AsyncMock()
    monkeypatch.setattr(notifier_watch.email_sender, "send_email", sent)
    await notifier_watch._alert_owner_down("no refresh_token", "farid@thaink2.com")
    sent.assert_awaited_once()
    # routed to the super admin, not through system_mailer
    assert sent.call_args.kwargs["to"]  # super_admin_email

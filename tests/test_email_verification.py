"""Tests for email verification (B-notif): token type, verify, resend anti-enum,
and the login gate behind AUTH_EMAIL_VERIFICATION_ENABLED."""

from unittest.mock import MagicMock, AsyncMock

import pytest
from fastapi import Response
from jose import jwt

from apowerb.auth import exceptions, service
from apowerb.helpers.security import get_algorithm, get_secret_key, get_password_hash


class _FakeUser:
    def __init__(self, email="alice@example.com", password="pw", email_verified=False):
        self.email = email
        self.password = password
        self.user_id = 1
        self.email_verified = email_verified
        self.mfa_enabled = False
        role = MagicMock()
        role.name = "USER"
        role.value = "USER"
        self.role = role


class _FakeResult:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


class _FakeSession:
    def __init__(self, user=None):
        self._user = user
        self.committed = False

    async def execute(self, stmt):
        return _FakeResult(self._user)

    async def commit(self):
        self.committed = True

    async def refresh(self, obj):
        return None


# ── token type ───────────────────────────────────────────────────────────────

def test_generate_verify_token_has_distinct_type():
    tok = service.generate_verify_token("alice@example.com")
    payload = jwt.decode(tok, get_secret_key(), algorithms=[get_algorithm()])
    assert payload["type"] == service.EMAIL_VERIFY_TOKEN_TYPE == "email_verify"
    assert payload["type"] != service.PASSWORD_RESET_TOKEN_TYPE
    assert payload["sub"] == "alice@example.com"


# ── verify_email_token ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_verify_email_token_marks_verified():
    user = _FakeUser(email_verified=False)
    db = _FakeSession(user)
    await service.verify_email_token(service.generate_verify_token(user.email), db)
    assert user.email_verified is True
    assert db.committed is True


@pytest.mark.asyncio
async def test_verify_email_token_idempotent_already_verified():
    user = _FakeUser(email_verified=True)
    db = _FakeSession(user)
    await service.verify_email_token(service.generate_verify_token(user.email), db)
    assert user.email_verified is True
    assert db.committed is False  # no-op, no extra commit


@pytest.mark.asyncio
async def test_verify_email_token_rejects_reset_token_type():
    # A password_reset token must NOT validate as an email-verify token.
    reset_tok = service.generate_reset_token("alice@example.com")
    with pytest.raises(exceptions.InvalidCredentials):
        await service.verify_email_token(reset_tok, _FakeSession(_FakeUser()))


@pytest.mark.asyncio
async def test_verify_email_token_unknown_user():
    with pytest.raises(exceptions.InvalidCredentials):
        await service.verify_email_token(
            service.generate_verify_token("ghost@example.com"), _FakeSession(None)
        )


# ── send_verification_email (anti-enumeration) ───────────────────────────────

@pytest.mark.asyncio
async def test_send_verification_unknown_email_is_silent(monkeypatch):
    sent = AsyncMock()
    monkeypatch.setattr("apowerb.helpers.system_mailer.send_system_email", sent)
    await service.send_verification_email("ghost@example.com", _FakeSession(None))
    sent.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_verification_already_verified_is_silent(monkeypatch):
    sent = AsyncMock()
    monkeypatch.setattr("apowerb.helpers.system_mailer.send_system_email", sent)
    await service.send_verification_email("alice@example.com", _FakeSession(_FakeUser(email_verified=True)))
    sent.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_verification_unverified_sends(monkeypatch):
    sent = AsyncMock()
    monkeypatch.setattr("apowerb.helpers.system_mailer.send_system_email", sent)
    await service.send_verification_email("alice@example.com", _FakeSession(_FakeUser(email_verified=False)))
    sent.assert_awaited_once()
    assert sent.call_args.kwargs["to"] == "alice@example.com"


# ── login gate behind the flag ───────────────────────────────────────────────

def _login_req(email="alice@example.com", password="GoodP@ss1"):
    return service.schemas.LoginRequest(email=email, password=password)


@pytest.mark.asyncio
async def test_login_gate_blocks_unverified_when_flag_on(monkeypatch):
    monkeypatch.setattr(service.settings, "auth_email_verification_enabled", True)
    user = _FakeUser(password=get_password_hash("GoodP@ss1"), email_verified=False)
    with pytest.raises(exceptions.EmailNotVerified):
        await service.login(_login_req(), Response(), _FakeSession(user))


@pytest.mark.asyncio
async def test_login_allows_verified_when_flag_on(monkeypatch):
    monkeypatch.setattr(service.settings, "auth_email_verification_enabled", True)
    user = _FakeUser(password=get_password_hash("GoodP@ss1"), email_verified=True)
    out = await service.login(_login_req(), Response(), _FakeSession(user))
    assert out.access_token


@pytest.mark.asyncio
async def test_login_ignores_verification_when_flag_off(monkeypatch):
    monkeypatch.setattr(service.settings, "auth_email_verification_enabled", False)
    user = _FakeUser(password=get_password_hash("GoodP@ss1"), email_verified=False)
    out = await service.login(_login_req(), Response(), _FakeSession(user))
    assert out.access_token  # gate disabled -> login succeeds despite unverified


# ── refresh-token path must honour the gate too (review Finding 2) ───────────

from datetime import datetime, timedelta, timezone


def _refresh_request(email="alice@example.com"):
    tok = jwt.encode(
        {"sub": email, "type": "refresh",
         "exp": datetime.now(timezone.utc) + timedelta(days=1)},
        service.settings.encrypt_key, algorithm=service.settings.algorithm,
    )
    return type("_Req", (), {"cookies": {"refresh_token": tok}})()


@pytest.mark.asyncio
async def test_refresh_blocks_unverified_when_flag_on(monkeypatch):
    monkeypatch.setattr(service.settings, "auth_email_verification_enabled", True)
    user = _FakeUser(email_verified=False)
    with pytest.raises(exceptions.InvalidCredentials):
        await service.refresh(_refresh_request(), _FakeSession(user))


@pytest.mark.asyncio
async def test_refresh_allows_verified_when_flag_on(monkeypatch):
    monkeypatch.setattr(service.settings, "auth_email_verification_enabled", True)
    user = _FakeUser(email_verified=True)
    out = await service.refresh(_refresh_request(), _FakeSession(user))
    assert out.access_token

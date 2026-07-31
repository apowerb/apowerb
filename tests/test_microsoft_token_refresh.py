"""Unit tests for the Microsoft access-token refresh helper.

Covers:
1. JWT exp decoding (valid / missing / malformed)
2. is_access_token_expired leeway behaviour
3. get_valid_access_token — 5 branches:
   - token is fresh → returns stored, no refresh, no commit
   - token expired + refresh OK → calls Microsoft, persists, returns new token
   - token expired + no refresh_token → raises IntegrationTokenExpiredError
   - token expired + Microsoft returns 400 (invalid_grant) → raises IntegrationTokenExpiredError
   - no integration row → raises IntegrationTokenExpiredError
"""

import base64
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apowerb.integrations.microsoft import (
    IntegrationTokenExpiredError,
    MicrosoftIntegrationService,
    _decode_jwt_exp,
    is_access_token_expired,
)


def _make_jwt(claims: dict) -> str:
    """Build a minimal JWT (alg:none) carrying the given claims — signature-free."""
    def _b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()
    header = _b64(b'{"alg":"none","typ":"JWT"}')
    payload = _b64(json.dumps(claims).encode())
    return f"{header}.{payload}.sig"


def _db_with_integration(integration) -> AsyncMock:
    """Build an AsyncSession mock whose execute() returns a result whose
    scalar_one_or_none() yields the given integration (or None)."""
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = integration
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    return db


class TestDecodeJwtExp:
    def test_valid_exp_returned_as_int(self):
        token = _make_jwt({"exp": 1234567890, "sub": "x"})
        assert _decode_jwt_exp(token) == 1234567890

    def test_missing_exp_returns_none(self):
        token = _make_jwt({"sub": "x"})
        assert _decode_jwt_exp(token) is None

    def test_malformed_token_returns_none(self):
        assert _decode_jwt_exp("not-a-jwt") is None
        assert _decode_jwt_exp("only.two") is None

    def test_non_base64_payload_returns_none(self):
        assert _decode_jwt_exp("aaa.!!!.sig") is None


class TestIsAccessTokenExpired:
    def test_fresh_token_not_expired(self):
        token = _make_jwt({"exp": int(time.time()) + 3600})
        assert is_access_token_expired(token) is False

    def test_past_token_is_expired(self):
        token = _make_jwt({"exp": int(time.time()) - 60})
        assert is_access_token_expired(token) is True

    def test_within_leeway_treated_as_expired(self):
        # Token expires in 30s; with 60s leeway we must refresh early.
        token = _make_jwt({"exp": int(time.time()) + 30})
        assert is_access_token_expired(token, leeway_seconds=60) is True

    def test_outside_leeway_still_fresh(self):
        token = _make_jwt({"exp": int(time.time()) + 300})
        assert is_access_token_expired(token, leeway_seconds=60) is False

    def test_none_and_empty_treated_as_expired(self):
        assert is_access_token_expired(None) is True
        assert is_access_token_expired("") is True

    def test_malformed_treated_as_expired(self):
        assert is_access_token_expired("garbage") is True


class TestGetValidAccessToken:
    @pytest.mark.asyncio
    async def test_fresh_token_returned_without_refresh(self):
        fresh = _make_jwt({"exp": int(time.time()) + 3600})
        integ = MagicMock(access_token=fresh, refresh_token="r")
        db = _db_with_integration(integ)

        with patch(
            "apowerb.integrations.microsoft.httpx.AsyncClient"
        ) as mock_client:
            token = await MicrosoftIntegrationService.get_valid_access_token(
                db, user_id=1, service="outlook"
            )

        assert token == fresh
        mock_client.assert_not_called()
        db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_expired_token_triggers_refresh_and_persists(self):
        expired = _make_jwt({"exp": int(time.time()) - 3600})
        new_token = _make_jwt({"exp": int(time.time()) + 3600})
        integ = MagicMock(access_token=expired, refresh_token="old_refresh")
        db = _db_with_integration(integ)

        resp = MagicMock(status_code=200)
        resp.json.return_value = {
            "access_token": new_token,
            "refresh_token": "rotated_refresh",
            "scope": "Mail.Read Mail.Read.Shared",
        }

        with patch(
            "apowerb.integrations.microsoft.httpx.AsyncClient"
        ) as mock_client_cls:
            client = AsyncMock()
            client.post = AsyncMock(return_value=resp)
            mock_client_cls.return_value.__aenter__.return_value = client

            token = await MicrosoftIntegrationService.get_valid_access_token(
                db, user_id=1, service="outlook"
            )

        assert token == new_token
        assert integ.access_token == new_token
        assert integ.refresh_token == "rotated_refresh"
        assert integ.scopes == "Mail.Read Mail.Read.Shared"
        db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_expired_without_refresh_token_raises(self):
        expired = _make_jwt({"exp": int(time.time()) - 3600})
        integ = MagicMock(access_token=expired, refresh_token="")
        db = _db_with_integration(integ)

        with pytest.raises(IntegrationTokenExpiredError):
            await MicrosoftIntegrationService.get_valid_access_token(
                db, user_id=1, service="outlook"
            )
        db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_refresh_call_failure_raises(self):
        expired = _make_jwt({"exp": int(time.time()) - 3600})
        integ = MagicMock(access_token=expired, refresh_token="old_refresh")
        db = _db_with_integration(integ)

        resp = MagicMock(status_code=400)
        resp.text = '{"error":"invalid_grant"}'

        with patch(
            "apowerb.integrations.microsoft.httpx.AsyncClient"
        ) as mock_client_cls:
            client = AsyncMock()
            client.post = AsyncMock(return_value=resp)
            mock_client_cls.return_value.__aenter__.return_value = client

            with pytest.raises(IntegrationTokenExpiredError):
                await MicrosoftIntegrationService.get_valid_access_token(
                    db, user_id=1, service="outlook"
                )

        db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_integration_raises(self):
        db = _db_with_integration(None)
        with pytest.raises(IntegrationTokenExpiredError):
            await MicrosoftIntegrationService.get_valid_access_token(
                db, user_id=999, service="outlook"
            )

    @pytest.mark.asyncio
    async def test_refresh_returns_no_access_token_raises(self):
        expired = _make_jwt({"exp": int(time.time()) - 3600})
        integ = MagicMock(access_token=expired, refresh_token="old_refresh")
        db = _db_with_integration(integ)

        resp = MagicMock(status_code=200)
        resp.json.return_value = {"token_type": "Bearer"}  # missing access_token

        with patch(
            "apowerb.integrations.microsoft.httpx.AsyncClient"
        ) as mock_client_cls:
            client = AsyncMock()
            client.post = AsyncMock(return_value=resp)
            mock_client_cls.return_value.__aenter__.return_value = client

            with pytest.raises(IntegrationTokenExpiredError):
                await MicrosoftIntegrationService.get_valid_access_token(
                    db, user_id=1, service="outlook"
                )

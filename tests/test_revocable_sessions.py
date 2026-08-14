"""Two demands an administrator can now actually make.

This guard sits on the path every authenticated request takes, so the
tests care as much about who must still get through as about who must be
stopped: a mistake in the first direction strands an account, a mistake in
the second locks out an install.
"""

import os

# Signing refuses an empty key, deliberately — the same guard that keeps a
# production install from minting tokens with no secret.
os.environ.setdefault("ENCRYPT_KEY", "test-only-key-not-used-anywhere-else")

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from apowerb.auth.dependencies import get_current_user
from apowerb.helpers.security import create_access_token, get_secret_key


def _request(path="/api/agents"):
    return SimpleNamespace(url=SimpleNamespace(path=path))


def _credentials(token):
    return SimpleNamespace(credentials=token)


def _db_returning(user):
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = user
    db.execute = AsyncMock(return_value=result)
    return db


def _user(**over):
    u = MagicMock()
    u.user_id = 7
    u.email = "someone@example.com"
    u.role = "USER"
    u.first_name = "So"
    u.last_name = "Meone"
    u.username = None
    u.full_name = None
    u.avatar_url = None
    u.plan = None
    u.stripe_customer_id = None
    u.created_at = datetime.now(timezone.utc)
    u.updated_at = datetime.now(timezone.utc)
    u.mfa_enabled = False
    u.mfa_required = False
    u.sessions_valid_from = None
    for k, v in over.items():
        setattr(u, k, v)
    return u


async def _call(user, path="/api/agents", token=None):
    token = token or create_access_token({"sub": user.email, "type": "access"})
    return await get_current_user(
        request=_request(path), credentials=_credentials(token), db=_db_returning(user)
    )


# --- force re-login -------------------------------------------------------


@pytest.mark.asyncio
async def test_a_token_older_than_the_cutoff_is_refused():
    user = _user(sessions_valid_from=datetime.now(timezone.utc) + timedelta(minutes=1))
    with pytest.raises(HTTPException) as exc:
        await _call(user)
    assert exc.value.status_code == 401
    # A distinctive detail: the front has to tell "sign in again" apart from
    # "your credentials are wrong".
    assert exc.value.detail == "session_revoked"


@pytest.mark.asyncio
async def test_a_token_minted_after_the_cutoff_gets_through():
    user = _user(sessions_valid_from=datetime.now(timezone.utc) - timedelta(minutes=1))
    out = await _call(user)
    assert out.email == "someone@example.com"


@pytest.mark.asyncio
async def test_a_token_with_no_iat_is_treated_as_older():
    """Tokens minted before this shipped carry no `iat`. An administrator
    who revokes wants them gone, so the unknown case resolves to refused.
    """
    from jose import jwt

    from apowerb.helpers.security import get_algorithm

    token = jwt.encode(
        {
            "sub": "someone@example.com",
            "type": "access",
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        get_secret_key(),
        algorithm=get_algorithm(),
    )
    user = _user(sessions_valid_from=datetime.now(timezone.utc) - timedelta(days=1))

    with pytest.raises(HTTPException) as exc:
        await _call(user, token=token)
    assert exc.value.detail == "session_revoked"


@pytest.mark.asyncio
async def test_never_revoked_is_the_common_case_and_costs_nothing():
    out = await _call(_user(sessions_valid_from=None))
    assert out.email == "someone@example.com"


# --- require MFA ----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_required_factor_that_is_missing_blocks_the_product():
    user = _user(mfa_required=True, mfa_enabled=False)
    with pytest.raises(HTTPException) as exc:
        await _call(user, path="/api/agents")
    assert exc.value.status_code == 403
    assert exc.value.detail == "mfa_enrolment_required"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/api/auth/mfa/enable",
        "/api/auth/mfa/setup",
        "/api/auth/mfa/status",
        "/api/users/me",
        "/api/auth/logout",
    ],
)
async def test_enrolment_stays_reachable(path):
    """Refusing everything would deadlock: enrolling requires being signed
    in. These are the routes that let someone satisfy the demand.
    """
    user = _user(mfa_required=True, mfa_enabled=False)
    out = await _call(user, path=path)
    assert out.email == "someone@example.com"


@pytest.mark.asyncio
async def test_once_enrolled_the_demand_is_satisfied():
    user = _user(mfa_required=True, mfa_enabled=True)
    out = await _call(user, path="/api/agents")
    assert out.email == "someone@example.com"


@pytest.mark.asyncio
async def test_an_account_with_neither_flag_is_untouched():
    out = await _call(_user(), path="/api/agents")
    assert out.email == "someone@example.com"

"""Two demands an administrator can now actually make.

This guard sits on the path every authenticated request takes, so the
tests care as much about who must still get through as about who must be
stopped: a mistake in the first direction strands an account, a mistake in
the second locks out an install.
"""

import os

# Signing refuses an empty key, deliberately — the same guard that keeps a
# production install from minting tokens with no secret.
#
# Setting the environment variable is not enough on its own: `get_settings()`
# is cached, so whether this module's assignment is seen depends on which
# test module imported first. Run alone the suite passed; run beside another
# it failed on all 24. The fixture below settles it on the object itself.
os.environ.setdefault("ENCRYPT_KEY", "test-only-key-not-used-anywhere-else")

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from apowerb.auth.dependencies import get_current_user
from apowerb.helpers.security import create_access_token, get_secret_key


@pytest.fixture(autouse=True)
def _signing_key():
    """Guarantee a usable key whatever order the modules were imported in."""
    from apowerb.configs.settings import get_settings

    settings = get_settings()
    previous = settings.encrypt_key
    if not previous:
        settings.encrypt_key = os.environ["ENCRYPT_KEY"]
    yield
    settings.encrypt_key = previous


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


# --- the guards must fire on their state and on nothing else --------------
#
# CI found both of these: a fake user row whose new attributes are mocks is
# neither None nor False, so the cut-off refused a good token and the gate
# locked the account out of the product. This sits on the path of every
# authenticated request, so the failure mode matters as much as the feature.


@pytest.mark.asyncio
@pytest.mark.parametrize("junk", [object(), "2026-01-01", 1, MagicMock()])
async def test_a_cutoff_that_is_not_a_datetime_revokes_nobody(junk):
    """Failing closed here would sign out an install nobody asked to sign
    out, and the comparison itself could raise inside the dependency."""
    out = await _call(_user(sessions_valid_from=junk))
    assert out.email == "someone@example.com"


@pytest.mark.asyncio
async def test_a_naive_cutoff_is_read_as_utc_rather_than_raising():
    """Comparing a naive value to an aware `iat` raises TypeError — inside
    the auth dependency, on every request."""
    naive = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=1)
    with pytest.raises(HTTPException) as exc:
        await _call(_user(sessions_valid_from=naive))
    assert exc.value.detail == "session_revoked"


@pytest.mark.asyncio
@pytest.mark.parametrize("junk", ["yes", 1, MagicMock()])
async def test_only_a_real_true_demands_a_second_factor(junk):
    out = await _call(_user(mfa_required=junk, mfa_enabled=False), path="/api/agents")
    assert out.email == "someone@example.com"


@pytest.mark.asyncio
async def test_a_mocked_mfa_enabled_does_not_satisfy_a_real_demand():
    """The other direction: something merely truthy must not pass for an
    enrolled second factor."""
    with pytest.raises(HTTPException) as exc:
        await _call(_user(mfa_required=True, mfa_enabled=MagicMock()), path="/api/agents")
    assert exc.value.detail == "mfa_enrolment_required"


# --- the token and the cut-off do not have the same resolution -----------
#
# A JWT `iat` is a whole number of seconds; the cut-off is a database
# timestamp with microseconds. Compared as-is, the fresh token a user gets
# when they immediately sign back in is refused — proven on the running
# install before this was fixed: revoke, sign in again, still 401.


@pytest.mark.asyncio
async def test_signing_back_in_right_after_a_revocation_works():
    """The whole point of "force re-login" is that they can sign in again."""
    revoked_at = datetime.now(timezone.utc).replace(microsecond=500_000)
    user = _user(sessions_valid_from=revoked_at)

    # The token they get one second later, as `create_access_token` stamps it.
    token = create_access_token(
        {"sub": user.email, "type": "access", "iat": revoked_at + timedelta(seconds=1)}
    )

    out = await _call(user, token=token)
    assert out.email == "someone@example.com"


@pytest.mark.asyncio
async def test_a_token_stamped_with_the_very_second_of_the_revocation_is_refused():
    """It may have been minted a fraction before the click, and nothing
    distinguishes the two. Rounding down would let it survive — which is
    the one thing this exists to prevent."""
    revoked_at = datetime.now(timezone.utc).replace(microsecond=500_000)
    token = create_access_token(
        {
            "sub": "someone@example.com",
            "type": "access",
            "iat": revoked_at.replace(microsecond=0),
        }
    )

    with pytest.raises(HTTPException) as exc:
        await _call(_user(sessions_valid_from=revoked_at), token=token)
    assert exc.value.detail == "session_revoked"


@pytest.mark.asyncio
async def test_a_whole_second_cutoff_is_left_alone():
    """No rounding to do, and none applied: a token stamped with exactly
    that second is not older than it."""
    revoked_at = datetime.now(timezone.utc).replace(microsecond=0)
    token = create_access_token(
        {"sub": "someone@example.com", "type": "access", "iat": revoked_at}
    )

    out = await _call(_user(sessions_valid_from=revoked_at), token=token)
    assert out.email == "someone@example.com"

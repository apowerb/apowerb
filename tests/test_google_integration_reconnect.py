"""Regression tests — Gmail stuck on INTEGRATION_MISSING after reconnect.

Incident 2026-07-23 (dev): the first ``tool_send_email`` ran before the
user had connected google_gmail. The failed DB load was latched in
``google_auth._integration_loaded_for`` (the ``finally`` treated failure
as "loaded"), and the OAuth-callback reset crashed silently on a stale
attribute name (``_integration_loaded``, renamed in PR #86). Every
subsequent tool call skipped the DB refetch and returned
INTEGRATION_MISSING until process restart — reconnecting did nothing.
"""

import os

import pytest

from apowerb.core.invocation_context import set_current_invoker
from apowerb.tools_store.portfolio import google_auth

INVOKER = "elom.gnaglo@example.com"
ENV_KEY = "GOOGLE_GMAIL_REFRESH_TOKEN"


@pytest.fixture()
def bound_invoker(monkeypatch):
    monkeypatch.setattr(google_auth, "_integration_loaded_for", {})
    monkeypatch.setattr(google_auth, "_token_cache", {})
    monkeypatch.delenv(ENV_KEY, raising=False)
    set_current_invoker(INVOKER)
    yield INVOKER
    set_current_invoker(None)


def test_failed_load_is_not_latched_and_recovers_once_user_connects(
    bound_invoker, monkeypatch
):
    """A tool call made BEFORE the user connects must not poison the loader:
    once the integration row exists, the very next call must pick it up."""
    calls: list[str] = []

    def _fetch_no_row(provider, user=None):
        calls.append(provider)
        raise RuntimeError(
            f"No {provider} integration found for user_id=9. "
            "The user must connect via the Integrations page first."
        )

    monkeypatch.setattr(
        "apowerb.integrations.helpers.fetch_integration_configs", _fetch_no_row
    )
    google_auth._ensure_integration_tokens("GOOGLE_GMAIL")

    assert os.environ.get(ENV_KEY) is None
    assert google_auth._integration_loaded_for.get("GOOGLE_GMAIL") is None

    # User connects via the Integrations page — the row now exists.
    def _fetch_connected(provider, user=None):
        calls.append(provider)
        return {"access_token": "at", "refresh_token": "rt-after-connect", "meta": {}}

    monkeypatch.setattr(
        "apowerb.integrations.helpers.fetch_integration_configs", _fetch_connected
    )
    google_auth._ensure_integration_tokens("GOOGLE_GMAIL")

    assert calls == ["google_gmail", "google_gmail"]
    assert os.environ.get(ENV_KEY) == "rt-after-connect"
    assert google_auth._integration_loaded_for.get("GOOGLE_GMAIL") == INVOKER


def test_other_invokers_failed_load_does_not_strand_a_connected_user(
    bound_invoker, monkeypatch
):
    """User B (not connected) invoking on the same worker pops user A's env
    token via the invoker-changed branch. A's latch must go with it, or A's
    next call early-returns with no token — INTEGRATION_MISSING for a user
    who IS connected, triggered by someone else's failure."""

    def _fetch_user_a(provider, user=None):
        return {"access_token": "at", "refresh_token": "rt-user-a", "meta": {}}

    def _fetch_no_row(provider, user=None):
        raise RuntimeError("No google_gmail integration found for user_id=42.")

    monkeypatch.setattr(
        "apowerb.integrations.helpers.fetch_integration_configs", _fetch_user_a
    )
    google_auth._ensure_integration_tokens("GOOGLE_GMAIL")
    assert os.environ.get(ENV_KEY) == "rt-user-a"

    set_current_invoker("user-b@example.com")
    monkeypatch.setattr(
        "apowerb.integrations.helpers.fetch_integration_configs", _fetch_no_row
    )
    google_auth._ensure_integration_tokens("GOOGLE_GMAIL")
    assert os.environ.get(ENV_KEY) is None

    set_current_invoker(INVOKER)
    monkeypatch.setattr(
        "apowerb.integrations.helpers.fetch_integration_configs", _fetch_user_a
    )
    google_auth._ensure_integration_tokens("GOOGLE_GMAIL")
    assert os.environ.get(ENV_KEY) == "rt-user-a"


def test_oauth_callback_reset_clears_latch_and_forces_db_refetch(
    bound_invoker, monkeypatch
):
    """The reset run by /google/callback (and DELETE /{provider}) must clear
    the lazy-load latch so the next tool call refetches from the DB."""
    from apowerb.routers.integrations import _reset_google_gmail_module_state

    google_auth._integration_loaded_for["GOOGLE_GMAIL"] = INVOKER
    monkeypatch.setenv(ENV_KEY, "stale-token")

    _reset_google_gmail_module_state()

    assert os.environ.get(ENV_KEY) is None
    assert google_auth._integration_loaded_for == {}
    assert google_auth._token_cache == {}

    def _fetch_fresh(provider, user=None):
        return {"access_token": "at", "refresh_token": "rt-fresh", "meta": {}}

    monkeypatch.setattr(
        "apowerb.integrations.helpers.fetch_integration_configs", _fetch_fresh
    )
    google_auth._ensure_integration_tokens("GOOGLE_GMAIL")

    assert os.environ.get(ENV_KEY) == "rt-fresh"

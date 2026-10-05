"""Two users of the same shared agent must not share OneDrive/Teams tokens.

* ``teams.py`` self-resolves its token per invoker via ``microsoft_auth``
  (TEAMS is in ``_SELF_RESOLVE_PREFIXES``), so a poisoned process-global is
  never trusted.
* ``onedrive_core.py`` now delegates its token exchange to the same
  invoker-scoped ``microsoft_auth`` helper. The agent path no longer writes a
  process-global ``ONEDRIVE_REFRESH_TOKEN`` (that global races across
  concurrent invocations on the single worker — incident 2026-07-03), yet an
  ``env_scope`` caller that sets the global deliberately is still honoured
  (ONEDRIVE resolves env-first).

Plus: the OneDrive disconnect reset must actually drop the cached token state
(it previously imported a module that does not exist and cleared nothing).
"""

import os
from unittest.mock import patch

import pytest

import apowerb.tools_store.portfolio.microsoft_auth as ma
import apowerb.tools_store.portfolio.onedrive_core as oc
from apowerb.core import invocation_context as ic
from apowerb.integrations import helpers as ih


@pytest.fixture(autouse=True)
def _reset_state():
    """Reset invoker ContextVar and the invoker-scoped token cache between tests."""
    yield
    ic.set_current_invoker(None)
    ma._token_cache.clear()
    os.environ.pop("TEAMS_REFRESH_TOKEN", None)
    os.environ.pop("ONEDRIVE_REFRESH_TOKEN", None)
    os.environ.pop("ONEDRIVE_CLIENT_ID", None)
    os.environ.pop("ONEDRIVE_CLIENT_SECRET", None)


def _fake_fetch(provider=None, user=None):
    # Mirror production: the row fetched belongs to the invoker.
    who = ic.resolve_integration_user(prefer_invoker=True)
    return {"refresh_token": f"rt-{who}"}


def _bind_shared_agent():
    ic.bind_agent_identity(
        owner="bob@corp",
        organization_id=None,
        project_id=None,
        agent_id="ag1",
        invocation_id="inv1",
    )


def _post_ok(refresh="rotated"):
    from unittest.mock import MagicMock

    resp = MagicMock()
    resp.status_code = 200
    resp.text = ""
    resp.json.return_value = {
        "access_token": "at",
        "refresh_token": refresh,
        "scope": "offline_access Files.ReadWrite",
    }
    return resp


def test_teams_does_not_trust_a_poisoned_global(monkeypatch):
    """TEAMS must resolve the invoker's own token from the DB, never a global
    env var a concurrent/previous invoker poisoned."""
    monkeypatch.setattr(ih, "fetch_integration_configs", _fake_fetch)
    _bind_shared_agent()
    ic.set_current_invoker("carol@corp")
    # A previous invoker (or an env_scope caller) left the process-global set:
    monkeypatch.setenv("TEAMS_REFRESH_TOKEN", "rt-alice@corp")

    resolved = ma._resolve_refresh_token("TEAMS")
    assert resolved == "rt-carol@corp", (
        f"Teams trusted a poisoned process-global token: {resolved!r}"
    )


def test_onedrive_agent_path_does_not_write_the_global(monkeypatch):
    """On the agent path (no env_scope), exchanging a OneDrive token must NOT
    write the process-global ONEDRIVE_REFRESH_TOKEN — that global races across
    concurrent invocations on the single worker."""
    monkeypatch.setattr(ih, "fetch_integration_configs", _fake_fetch)
    monkeypatch.setattr(ih, "persist_refreshed_tokens", lambda *a, **k: None)
    monkeypatch.setenv("ONEDRIVE_CLIENT_ID", "cid")
    monkeypatch.setenv("ONEDRIVE_CLIENT_SECRET", "csec")
    monkeypatch.delenv("ONEDRIVE_REFRESH_TOKEN", raising=False)
    _bind_shared_agent()
    ic.set_current_invoker("alice@corp")

    with patch.object(ma.httpx, "post", return_value=_post_ok()):
        tok = oc._get_access_token()

    assert tok == "at"
    assert "ONEDRIVE_REFRESH_TOKEN" not in os.environ, (
        "the token path poisoned the process-global"
    )


def test_onedrive_env_scope_token_is_honored_and_rotated(monkeypatch):
    """An env_scope caller sets ONEDRIVE_REFRESH_TOKEN deliberately; that token
    must be used, and the rotated value written back into the global so the
    env_scope block keeps a fresh token."""
    monkeypatch.setattr(ih, "persist_refreshed_tokens", lambda *a, **k: None)
    monkeypatch.setenv("ONEDRIVE_CLIENT_ID", "cid")
    monkeypatch.setenv("ONEDRIVE_CLIENT_SECRET", "csec")
    monkeypatch.setenv("ONEDRIVE_REFRESH_TOKEN", "env-token")

    captured = {}

    def _post(url, data=None, **k):
        captured["refresh_token"] = data["refresh_token"]
        return _post_ok(refresh="rotated")

    with patch.object(ma.httpx, "post", side_effect=_post):
        oc._get_access_token()

    assert captured["refresh_token"] == "env-token", "env_scope token was not used"
    assert os.environ.get("ONEDRIVE_REFRESH_TOKEN") == "rotated", (
        "env_scope global was not refreshed with the rotated token"
    )


def test_onedrive_two_invokers_get_their_own_token(monkeypatch):
    """Two invokers of a shared agent each resolve their own refresh token —
    no shared process-global on the agent path."""
    monkeypatch.setattr(ih, "fetch_integration_configs", _fake_fetch)
    monkeypatch.setattr(ih, "persist_refreshed_tokens", lambda *a, **k: None)
    monkeypatch.setenv("ONEDRIVE_CLIENT_ID", "cid")
    monkeypatch.setenv("ONEDRIVE_CLIENT_SECRET", "csec")
    monkeypatch.delenv("ONEDRIVE_REFRESH_TOKEN", raising=False)
    _bind_shared_agent()

    sent = []

    def _post(url, data=None, **k):
        sent.append(data["refresh_token"])
        return _post_ok(refresh=None)

    with patch.object(ma.httpx, "post", side_effect=_post):
        ic.set_current_invoker("alice@corp")
        oc._get_access_token()
        ic.set_current_invoker("carol@corp")
        oc._get_access_token()

    assert sent == ["rt-alice@corp", "rt-carol@corp"], (
        f"invokers shared a refresh token: {sent}"
    )
    assert "ONEDRIVE_REFRESH_TOKEN" not in os.environ


def test_onedrive_disconnect_actually_clears_token_state():
    """The disconnect/reconnect reset must really drop the cached token state
    (it previously imported a non-existent module and cleared nothing)."""
    from apowerb.routers import integrations as ir

    ma._token_cache[("ONEDRIVE", "u@x")] = {"access_token": "x", "expires_at": 9e18}
    os.environ["ONEDRIVE_REFRESH_TOKEN"] = "stale"

    ir._reset_onedrive_module_state()

    assert ma._token_cache == {}, "reset left the access-token cache populated"
    assert "ONEDRIVE_REFRESH_TOKEN" not in os.environ, "reset left the env token set"


def test_onedrive_env_scope_isolates_by_token_when_invoker_unset(monkeypatch):
    """Under env_scope the token comes from the process-global and the invoker
    may be unset/constant across different users (background trigger poller).
    The access-token cache must still isolate them by the token itself — one
    user's access token must never be served to the next."""
    monkeypatch.setattr(ih, "persist_refreshed_tokens", lambda *a, **k: None)
    monkeypatch.setenv("ONEDRIVE_CLIENT_ID", "cid")
    monkeypatch.setenv("ONEDRIVE_CLIENT_SECRET", "csec")
    ic.set_current_invoker(None)  # background run: invoker ContextVar unset

    sent = []

    def _post(url, data=None, **k):
        sent.append(data["refresh_token"])
        resp = _post_ok(refresh=None)
        resp.json.return_value = {"access_token": f"AT-{data['refresh_token']}", "refresh_token": None}
        return resp

    with patch.object(ma.httpx, "post", side_effect=_post):
        monkeypatch.setenv("ONEDRIVE_REFRESH_TOKEN", "rt-A")
        tok_a = oc._get_access_token()
        monkeypatch.setenv("ONEDRIVE_REFRESH_TOKEN", "rt-B")
        tok_b = oc._get_access_token()

    assert sent == ["rt-A", "rt-B"], f"a second env token reused the first's cache entry: {sent}"
    assert tok_a == "AT-rt-A" and tok_b == "AT-rt-B", (
        f"env_scope caller got the wrong user's access token: {tok_a!r} / {tok_b!r}"
    )

"""Two users of the same shared agent must not share OneDrive/Teams tokens.

Both tools lazily load an OAuth refresh token into a process-global
(``os.environ[...]``) and gate the reload on the wrong identity:

* ``teams.py`` loads once behind a module-global boolean and never re-scopes;
  ``microsoft_auth`` then resolves TEAMS *env-first*, so a later invoker reads
  the first invoker's poisoned global.
* ``onedrive_core.py`` reloads only when ``get_agent_owner()`` changes; on a
  shared agent the owner is constant, so a second invoker keeps the first's
  token.

Plus: the OneDrive disconnect reset imported a module that does not exist
(``...portfolio.onedrive``), so it silently cleared nothing.
"""

import os

import pytest

import apowerb.tools_store.portfolio.microsoft_auth as ma
import apowerb.tools_store.portfolio.onedrive_core as oc
import apowerb.tools_store.portfolio.teams as teams_tool
from apowerb.core import invocation_context as ic
from apowerb.integrations import helpers as ih


@pytest.fixture(autouse=True)
def _reset_state():
    """Reset invoker ContextVar and the module-level token state between tests."""
    yield
    ic.set_current_invoker(None)
    oc._integration_loaded_for = None
    oc._token_cache.clear()
    ma._token_cache.clear()
    os.environ.pop("TEAMS_REFRESH_TOKEN", None)
    os.environ.pop("ONEDRIVE_REFRESH_TOKEN", None)


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


def test_onedrive_does_not_leak_between_invokers_on_shared_agent(monkeypatch):
    """On a shared agent (owner constant), each invoker must load its own token."""
    monkeypatch.setattr(ih, "fetch_integration_configs", _fake_fetch)
    _bind_shared_agent()

    ic.set_current_invoker("alice@corp")
    oc._ensure_integration_tokens()
    assert os.environ.get("ONEDRIVE_REFRESH_TOKEN") == "rt-alice@corp"

    ic.set_current_invoker("carol@corp")
    oc._ensure_integration_tokens()
    assert os.environ.get("ONEDRIVE_REFRESH_TOKEN") == "rt-carol@corp", (
        "a second invoker kept the first invoker's OneDrive token"
    )


def test_onedrive_disconnect_actually_clears_token_state():
    """The disconnect/reconnect reset must really drop the cached token state
    (it previously imported a non-existent module and cleared nothing)."""
    from apowerb.routers import integrations as ir

    oc._integration_loaded_for = "bob@corp"
    oc._token_cache["k"] = {"access_token": "x", "expires_at": 9e18}
    os.environ["ONEDRIVE_REFRESH_TOKEN"] = "stale"

    ir._reset_onedrive_module_state()

    assert oc._integration_loaded_for is None, "reset left the loaded-owner flag set"
    assert oc._token_cache == {}, "reset left the access-token cache populated"
    assert "ONEDRIVE_REFRESH_TOKEN" not in os.environ, "reset left the env token set"

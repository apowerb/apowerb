"""Two users of the same shared agent must not share Odoo credentials.

``tools_store/portfolio/odoo.py`` caches decrypted Odoo credentials per process.
The value is fetched for the *invoker*
(``resolve_integration_user(prefer_invoker=True)``), but the cache was keyed by
the *agent owner* (``get_agent_owner()``). On a shared agent (owner = Bob,
invoked by Alice then Carol) the first invoker's credentials were cached under
the owner's key and then handed to every later invoker -- a cross-tenant
credential leak.
"""

import pytest

import apowerb.tools_store.portfolio.odoo as odoo_tool
from apowerb.core import invocation_context as ic
from apowerb.integrations import helpers as ih


@pytest.fixture(autouse=True)
def _reset_invoker_after_test():
    """Keep the invoker ContextVar from leaking out of these tests.

    They call ``set_current_invoker(...)`` and never reset it; a value left
    bound in the process would make a later test (e.g. ``TestTools`` in
    ``test_odoo_integration``) resolve the wrong integration user, miss the
    seeded creds cache and try to reach the DB.
    """
    yield
    ic.set_current_invoker(None)


def _fake_fetch(provider="odoo", user=None):
    # Mirror production: the row fetched belongs to the invoker.
    who = ic.resolve_integration_user(prefer_invoker=True)
    return {
        "access_token": f"key-{who}",
        "provider_username": f"{who}@login",
        "provider_user_id": "1",
        "meta": {"url": "https://x.odoo.com", "database": "db"},
    }


def test_a_shared_agent_does_not_leak_odoo_creds_between_invokers(monkeypatch):
    odoo_tool._creds_cache.clear()
    monkeypatch.setattr(ih, "fetch_integration_configs", _fake_fetch)
    monkeypatch.setattr(odoo_tool, "decrypt_value", lambda v: v)

    # A shared agent owned by Bob, invoked by two different users in turn.
    ic.bind_agent_identity(
        owner="bob@corp",
        organization_id=None,
        project_id=None,
        agent_id="ag1",
        invocation_id="inv1",
    )

    ic.set_current_invoker("alice@corp")
    alice = odoo_tool._load_creds()
    assert alice["api_key"] == "key-alice@corp"

    ic.set_current_invoker("carol@corp")
    carol = odoo_tool._load_creds()
    assert carol["api_key"] == "key-carol@corp", (
        f"a second invoker received another user's Odoo credentials: {carol['api_key']!r}"
    )


async def test_disconnecting_odoo_clears_the_creds_cache():
    """Disconnect (and reconnect) must drop cached creds so a re-registered
    account never inherits the previous holder's credentials from the cache."""
    from unittest.mock import AsyncMock, MagicMock

    from apowerb.routers import integrations as ir

    odoo_tool._creds_cache.clear()
    odoo_tool._creds_cache["u@x"] = {"api_key": "stale"}

    integration = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = integration
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    db.delete = AsyncMock()
    db.commit = AsyncMock()
    user = MagicMock(user_id=1, email="u@x")

    await ir.disconnect_integration(provider="odoo", db=db, current_user=user)
    assert odoo_tool._creds_cache == {}, "disconnect must clear the Odoo creds cache"

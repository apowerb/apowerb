"""Tests de l'helper ``env_scope`` (scoping sécurisé d'``os.environ``).

Référence : ``review-security.md`` Critical C6 — fuite inter-tenants via
``os.environ`` global. L'helper ``env_scope`` permet d'injecter des
credentials dans l'environnement pour la durée d'un appel, sous verrou
asyncio, puis de les restaurer systématiquement — même en cas
d'exception.

Couvre :
- set/restore basique
- clean-up en cas d'exception
- absence de fuite persistante après sortie du scope
- serialization (lock) pour éviter la fuite entre deux scopes concurrents
- restauration de la valeur originelle (et pas suppression) quand la
  variable existait déjà avant
"""

from __future__ import annotations

import asyncio
import os

import pytest


# ---------------------------------------------------------------------------
# Fixture — isole l'os.environ de chaque test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _cleanup_env():
    """Ensure each test starts from a clean slate for the keys we touch."""
    keys = (
        "B6_TEST_KEY",
        "B6_TEST_KEY_A",
        "B6_TEST_KEY_B",
        "B6_PREEXISTING",
    )
    original = {k: os.environ.get(k) for k in keys}
    for k in keys:
        os.environ.pop(k, None)
    try:
        yield
    finally:
        for k, v in original.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEnvScopeBasic:
    async def test_sets_vars_inside_scope(self):
        from th2agent.helpers.env_scope import env_scope

        async with env_scope({"B6_TEST_KEY": "secret-value"}):
            assert os.environ.get("B6_TEST_KEY") == "secret-value"

    async def test_removes_vars_after_scope(self):
        from th2agent.helpers.env_scope import env_scope

        async with env_scope({"B6_TEST_KEY": "secret-value"}):
            pass

        assert "B6_TEST_KEY" not in os.environ

    async def test_restores_preexisting_value(self):
        from th2agent.helpers.env_scope import env_scope

        os.environ["B6_PREEXISTING"] = "original"
        async with env_scope({"B6_PREEXISTING": "overridden"}):
            assert os.environ["B6_PREEXISTING"] == "overridden"

        assert os.environ["B6_PREEXISTING"] == "original"


class TestEnvScopeExceptionSafety:
    async def test_cleans_up_on_exception(self):
        from th2agent.helpers.env_scope import env_scope

        with pytest.raises(RuntimeError, match="boom"):
            async with env_scope({"B6_TEST_KEY": "secret"}):
                assert os.environ["B6_TEST_KEY"] == "secret"
                raise RuntimeError("boom")

        assert "B6_TEST_KEY" not in os.environ


class TestEnvScopeConcurrency:
    async def test_scopes_do_not_leak_between_concurrent_tasks(self):
        """Two concurrent scopes acquiring the same lock must see each
        their own value and never observe the other's value inside their
        protected section. We sleep inside the scope to encourage the
        scheduler to interleave.
        """
        from th2agent.helpers.env_scope import env_scope

        lock = asyncio.Lock()
        observations: dict[str, list[str]] = {"A": [], "B": []}

        async def worker(name: str, value: str):
            for _ in range(10):
                async with env_scope({"B6_TEST_KEY": value}, lock=lock):
                    observations[name].append(os.environ.get("B6_TEST_KEY", ""))
                    # Yield to the event loop to let the other task try to run.
                    await asyncio.sleep(0)
                    observations[name].append(os.environ.get("B6_TEST_KEY", ""))

        await asyncio.gather(
            worker("A", "alice-secret"),
            worker("B", "bob-secret"),
        )

        assert all(v == "alice-secret" for v in observations["A"]), observations["A"]
        assert all(v == "bob-secret" for v in observations["B"]), observations["B"]
        # After everyone is done, the key must be gone.
        assert "B6_TEST_KEY" not in os.environ


class TestEnvScopeSkipsEmptyValues:
    async def test_none_value_does_not_set_env_var(self):
        """Passing {"KEY": None} should NOT create an env var set to "None"."""
        from th2agent.helpers.env_scope import env_scope

        async with env_scope({"B6_TEST_KEY": None}):
            assert "B6_TEST_KEY" not in os.environ

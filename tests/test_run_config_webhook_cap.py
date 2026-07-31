"""TDD - Correction 1 : cap max_llm_calls sur le chemin webhook.

Le webhook appelle run_adk_agent(run_mode="run") qui POST /run.
/run appelle runner.run_async() SANS run_config -> le patch _CappedRunConfig
n'est jamais applique sur ce chemin.

Correction : patch_runner_run_async enveloppe runner.run_async pour injecter
run_config si absent. apply_adk_run_config_patch patch aussi get_runner_async
pour appliquer ce wrap sur chaque runner retourne.

Note sur la signature ADK :
  Runner.run_async(**kwargs) -> AsyncGenerator
  Ce n'est PAS un async generator direct ; c'est une coroutine qui retourne
  un AsyncGenerator. Le wrapper est donc une fonction SYNC qui injecte le
  run_config dans les kwargs PUIS delegue au _orig_run_async original.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch



class TestWebhookPathCapped:
    """Verifie que le cap max_llm_calls couvre le chemin /run (webhook)."""

    def test_patch_runner_run_async_injects_run_config(self):
        """patch_runner_run_async enveloppe run_async pour injecter run_config.

        Le wrapper est SYNC (comme Runner.run_async) et injecte run_config
        dans les kwargs avant de deleguer au mock original.
        """
        from apowerb.core.agent_helpers.run_config_patch import patch_runner_run_async

        fake_runner = MagicMock()
        captured_kwargs = {}

        async def _empty_agen():
            return
            yield  # rend la fonction async generator

        async def mock_run_async(**kwargs):
            captured_kwargs.update(kwargs)
            return _empty_agen()

        fake_runner.run_async = mock_run_async

        with patch.dict(os.environ, {"LLM_MAX_CALLS": "25"}, clear=False):
            patch_runner_run_async(fake_runner)

        import asyncio

        async def run():
            # Le wrapper est SYNC : on appelle directement, on obtient la coroutine
            coro = fake_runner.run_async(
                user_id="u1",
                session_id="s1",
                new_message={"role": "user", "parts": [{"text": "hello"}]},
            )
            # La coroutine retourne un AsyncGenerator
            agen = await coro
            async for _ in agen:
                pass

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(run())
        finally:
            loop.close()

        rc = captured_kwargs.get("run_config")
        assert rc is not None, "run_config non injecte dans run_async"
        assert rc.max_llm_calls == 25, (
            f"max_llm_calls attendu=25, obtenu={rc.max_llm_calls}"
        )

    def test_existing_run_config_not_overwritten(self):
        """Si run_async est appele avec un run_config explicite, il n'est pas ecrase."""
        from apowerb.core.agent_helpers.run_config_patch import patch_runner_run_async
        from google.adk.agents.run_config import RunConfig

        fake_runner = MagicMock()
        captured_kwargs = {}

        async def _empty_agen():
            return
            yield

        async def mock_run_async(**kwargs):
            captured_kwargs.update(kwargs)
            return _empty_agen()

        fake_runner.run_async = mock_run_async
        patch_runner_run_async(fake_runner)

        import asyncio
        explicit_rc = RunConfig(max_llm_calls=99)

        async def run():
            coro = fake_runner.run_async(
                user_id="u1",
                session_id="s1",
                new_message={"role": "user", "parts": [{"text": "hello"}]},
                run_config=explicit_rc,
            )
            agen = await coro
            async for _ in agen:
                pass

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(run())
        finally:
            loop.close()

        rc = captured_kwargs.get("run_config")
        assert rc is explicit_rc, "run_config explicite ecrase par le wrapper"
        assert rc.max_llm_calls == 99

    def test_patch_runner_run_async_importable(self):
        """patch_runner_run_async est exportee depuis run_config_patch."""
        import apowerb.core.agent_helpers.run_config_patch as m
        assert hasattr(m, "patch_runner_run_async"), (
            "patch_runner_run_async manquante dans run_config_patch"
        )

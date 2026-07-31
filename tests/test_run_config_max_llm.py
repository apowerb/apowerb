"""TDD - Levier 3 : cap d'appels LLM via RunConfig(max_llm_calls).

Tests ecrits AVANT l'implementation. Objectif : limiter les appels LLM
a 25 par defaut (configurable via LLM_MAX_CALLS) pour adapter le
pipeline SCEI au contexte OVHcloud.

Architecture : le serveur ADK est patche au demarrage dans main.py via
un monkey-patch sur AdkWebServer._run_sse_core (ou equivalent) pour
injecter max_llm_calls dans le RunConfig.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch


class TestRunConfigMaxLlmCalls:
    """Tests du cap LLM_MAX_CALLS dans RunConfig."""

    def test_get_llm_max_calls_default(self):
        """Sans LLM_MAX_CALLS, la valeur par defaut est 25."""
        from apowerb.core.agent_helpers.run_config_patch import get_llm_max_calls
        env_without = {k: v for k, v in os.environ.items() if k != "LLM_MAX_CALLS"}
        with patch.dict(os.environ, env_without, clear=True):
            assert get_llm_max_calls() == 25

    def test_get_llm_max_calls_from_env(self):
        """LLM_MAX_CALLS=40 -> retourne 40."""
        from apowerb.core.agent_helpers.run_config_patch import get_llm_max_calls
        with patch.dict(os.environ, {"LLM_MAX_CALLS": "40"}, clear=False):
            assert get_llm_max_calls() == 40

    def test_get_llm_max_calls_invalid_env_uses_default(self):
        """LLM_MAX_CALLS=invalid -> fallback sur 25."""
        from apowerb.core.agent_helpers.run_config_patch import get_llm_max_calls
        with patch.dict(os.environ, {"LLM_MAX_CALLS": "not_a_number"}, clear=False):
            assert get_llm_max_calls() == 25

    def test_patch_run_config_injects_max_llm_calls(self):
        """patch_adk_run_config() patche l'objet RunConfig passe en argument."""
        from apowerb.core.agent_helpers.run_config_patch import patch_run_config

        # Simule un RunConfig avec max_llm_calls=500 (defaut ADK)
        fake_run_config = MagicMock()
        fake_run_config.max_llm_calls = 500

        with patch.dict(os.environ, {"LLM_MAX_CALLS": "25"}, clear=False):
            patch_run_config(fake_run_config)

        assert fake_run_config.max_llm_calls == 25

    def test_patch_run_config_respects_env(self):
        """patch_run_config utilise LLM_MAX_CALLS de l'environnement."""
        from apowerb.core.agent_helpers.run_config_patch import patch_run_config

        fake_run_config = MagicMock()
        fake_run_config.max_llm_calls = 500

        with patch.dict(os.environ, {"LLM_MAX_CALLS": "10"}, clear=False):
            patch_run_config(fake_run_config)

        assert fake_run_config.max_llm_calls == 10

    def test_run_config_patch_module_importable(self):
        """Le module run_config_patch est importable."""
        import apowerb.core.agent_helpers.run_config_patch as m
        assert hasattr(m, "get_llm_max_calls")
        assert hasattr(m, "patch_run_config")
        assert hasattr(m, "apply_adk_run_config_patch")

"""Levier 3 : cap d'appels LLM via RunConfig(max_llm_calls).

Patche le serveur ADK (ApiServer) pour injecter max_llm_calls
dans chaque RunConfig cree par les endpoints /run et /run_sse.

Usage dans main.py apres get_fast_api_app() :
    from apowerb.core.agent_helpers.run_config_patch import apply_adk_run_config_patch
    apply_adk_run_config_patch()
"""

from __future__ import annotations

import logging
import os

_logger = logging.getLogger("apowerb.run_config_patch")

_DEFAULT_MAX_LLM_CALLS = 25


def get_llm_max_calls() -> int:
    """Lit LLM_MAX_CALLS depuis l'environnement. Defaut : 25."""
    raw = os.getenv("LLM_MAX_CALLS", str(_DEFAULT_MAX_LLM_CALLS))
    try:
        return int(raw)
    except (ValueError, TypeError):
        _logger.warning(
            "[RUN_CONFIG_PATCH] LLM_MAX_CALLS=%r invalide, fallback sur %d",
            raw,
            _DEFAULT_MAX_LLM_CALLS,
        )
        return _DEFAULT_MAX_LLM_CALLS


def patch_run_config(run_config: object) -> None:
    """Injecte max_llm_calls dans un RunConfig existant."""
    max_calls = get_llm_max_calls()
    run_config.max_llm_calls = max_calls
    _logger.debug("[RUN_CONFIG_PATCH] max_llm_calls set to %d", max_calls)


def patch_runner_run_async(runner: object) -> None:
    """Enveloppe runner.run_async pour injecter run_config si absent.

    Le chemin webhook POST /run appelle runner.run_async() SANS run_config,
    donc _CappedRunConfig n'est jamais instanciee sur ce chemin. Cette
    fonction patche l'instance runner directement pour injecter un RunConfig
    avec max_llm_calls si l'appelant n'en fournit pas.

    Doit etre appele sur chaque runner retourne par get_runner_async().
    """
    try:
        from google.adk.agents.run_config import RunConfig
    except ImportError:
        _logger.warning("[RUN_CONFIG_PATCH] ADK non disponible, patch runner ignore")
        return

    _orig_run_async = runner.run_async

    def _capped_run_async(**kwargs):
        """Injecte run_config si absent, puis delègue au runner original.

        Runner.run_async est une coroutine -> AsyncGenerator (pas un async gen
        direct). On doit donc appeler _orig_run_async(**kwargs) et retourner
        le resultat directement apres avoir injecte run_config dans les kwargs.
        """
        if kwargs.get("run_config") is None:
            kwargs["run_config"] = RunConfig(max_llm_calls=get_llm_max_calls())
            _logger.debug(
                "[RUN_CONFIG_PATCH] run_async: run_config injecte (max_llm_calls=%d)",
                get_llm_max_calls(),
            )
        return _orig_run_async(**kwargs)

    runner.run_async = _capped_run_async


def apply_adk_run_config_patch() -> None:
    """Monkey-patche ApiServer pour injecter max_llm_calls dans RunConfig.

    1) Remplace RunConfig dans api_server par _CappedRunConfig : couvre /run_sse.
    2) Patche ApiServer.get_runner_async pour appliquer patch_runner_run_async
       sur chaque runner retourne : couvre le chemin /run (webhook).

    Cible api_server et non adk_web_server : depuis ADK 2.x, AdkWebServer
    n'est qu'une sous-classe vide et depreciee de DevServer, et
    get_fast_api_app(web=False) -- notre appel -- instancie ApiServer.
    Patcher l'ancien module ne levait rien et ne plafonnait plus rien :
    le log annoncait meme un succes.

    Cette fonction doit etre appelee UNE FOIS apres get_fast_api_app()
    dans main.py.
    """
    try:
        from google.adk.agents.run_config import RunConfig
        import google.adk.cli.api_server as _aws_mod
    except ImportError as e:
        _logger.warning(
            "[RUN_CONFIG_PATCH] ADK non disponible, patch non applique : %s", e
        )
        return

    _orig_RunConfig = RunConfig

    class _CappedRunConfig(_orig_RunConfig):
        """RunConfig avec max_llm_calls plafonne a LLM_MAX_CALLS."""

        def __init__(self, **kwargs):
            if "max_llm_calls" not in kwargs:
                kwargs["max_llm_calls"] = get_llm_max_calls()
            super().__init__(**kwargs)

    # Patch dans le module api_server (ou il est importe)
    _aws_mod.RunConfig = _CappedRunConfig

    # Patch ApiServer.get_runner_async pour couvrir le chemin /run (webhook).
    # /run appelle runner.run_async() SANS run_config -> patch_runner_run_async
    # injecte le run_config avant chaque appel si l'appelant ne le fournit pas.
    _ServerClass = _aws_mod.ApiServer
    _orig_get_runner_async = _ServerClass.get_runner_async
    _PATCHED_ATTR = "_th2_run_config_patched"

    async def _patched_get_runner_async(self, app_name: str):
        runner = await _orig_get_runner_async(self, app_name)
        if not getattr(runner, _PATCHED_ATTR, False):
            patch_runner_run_async(runner)
            setattr(runner, _PATCHED_ATTR, True)
            _logger.debug(
                "[RUN_CONFIG_PATCH] runner.run_async patche pour %s", app_name
            )
        return runner

    _ServerClass.get_runner_async = _patched_get_runner_async

    _logger.info(
        "[RUN_CONFIG_PATCH] ApiServer.RunConfig patche -> max_llm_calls=%d",
        get_llm_max_calls(),
    )

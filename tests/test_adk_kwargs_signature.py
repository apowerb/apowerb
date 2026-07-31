"""TDD - Hotfix : signature kwargs ADK pour les wrappers chaînés.

ADK appelle before_model_callback EXCLUSIVEMENT par kwargs :
  callback(callback_context=ctx, llm_request=req)
(source : base_llm_flow.py dans le venv ADK, ligne ~228)

Ce test :
1. Prouve que la signature positionnelle (ctx, req) produit TypeError quand
   appelée par ADK (kwargs) — c'est le bug exact de la régression.
2. Prouve que la signature corrigée (*, callback_context, llm_request) fonctionne.
3. Teste les sous-callbacks réels (create_truncate_history_callback,
   create_strip_large_payloads_callback) avec l'appel ADK réel.

Import via importlib pour contourner agent_helpers/__init__.py qui connecte la DB.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest


_SRC = pathlib.Path(__file__).parent.parent / "src"


def _load_module(rel_path: str, name: str):
    """Charge un module Python directement par chemin fichier, sans passer
    par __init__.py du package parent."""
    full = _SRC / rel_path
    spec = importlib.util.spec_from_file_location(name, str(full))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _get_callbacks():
    return _load_module(
        "apowerb/core/agent_helpers/callbacks.py",
        "_apowerb_callbacks_direct",
    )


def _get_history_compaction():
    return _load_module(
        "apowerb/core/history_compaction.py",
        "_apowerb_history_compaction_direct",
    )


def _mk_content(role: str) -> Any:
    return SimpleNamespace(role=role, parts=[])


def _mk_request(n: int) -> Any:
    req = MagicMock()
    req.contents = [_mk_content("user")] + [_mk_content("model") for _ in range(n - 1)]
    return req


# ---------------------------------------------------------------------------
# Tests RED : prouver que la signature positionnelle (ctx, req) crash
# quand ADK appelle via kwargs
# ---------------------------------------------------------------------------

class TestPositionalSignatureCrashesOnAdkCall:
    """Prouve le bug exact : signature positionnelle + appel ADK par kwargs = TypeError."""

    def test_positional_wrapper_crashes_when_called_by_adk_kwargs(self):
        """Reproduit exactement le bug de production.

        chained_with_strip(ctx, req) + appel ADK callback_context=..., llm_request=...
        -> TypeError: got an unexpected keyword argument 'callback_context'

        Ce test doit PASSER (il prouve que TypeError est levé par le code bugué).
        """
        mod_cb = _get_callbacks()
        truncate_cb = mod_cb.create_truncate_history_callback(keep_recent=5)

        # Reproduire EXACTEMENT la signature buguée de agent_utils.py actuel
        # (avant le fix)
        def chained_with_strip_BUGGY(ctx, req, _trunc=truncate_cb):
            _trunc(callback_context=ctx, llm_request=req)
            return None

        fake_ctx = MagicMock()
        req = _mk_request(20)

        # ADK appelle via kwargs -> TypeError attendu avec le code bugué
        with pytest.raises(TypeError, match="unexpected keyword argument"):
            chained_with_strip_BUGGY(callback_context=fake_ctx, llm_request=req)

    def test_positional_chained_with_truncate_crashes_on_adk_kwargs(self):
        """Reproduit le bug de chained_with_truncate : signature (ctx, req)
        + appel ADK par kwargs -> TypeError."""
        mod_hc = _get_history_compaction()
        strip_cb = mod_hc.create_strip_large_payloads_callback("test_agent")

        def chained_with_truncate_BUGGY(ctx, req, _ex=strip_cb):
            _ex(ctx, req)  # strip veut (*, callback_context, llm_request) -> TypeError
            return None

        fake_ctx = MagicMock()
        req = _mk_request(20)

        # Deux TypeError possibles : appel positionnel au wrapper OU au sous-callback
        with pytest.raises(TypeError):
            chained_with_truncate_BUGGY(callback_context=fake_ctx, llm_request=req)

    def test_truncate_only_buggy_crashes_on_adk_kwargs(self):
        """Reproduit le bug de _truncate_only : signature positionnelle."""
        mod_cb = _get_callbacks()
        truncate_cb = mod_cb.create_truncate_history_callback(keep_recent=5)

        def _truncate_only_BUGGY(ctx, req, _trunc=truncate_cb):
            return _trunc(callback_context=ctx, llm_request=req)

        fake_ctx = MagicMock()
        req = _mk_request(20)

        with pytest.raises(TypeError, match="unexpected keyword argument"):
            _truncate_only_BUGGY(callback_context=fake_ctx, llm_request=req)


# ---------------------------------------------------------------------------
# Tests GREEN : prouver que la signature corrigée fonctionne
# ---------------------------------------------------------------------------

class TestFixedSignatureWorksWithAdkKwargs:
    """Prouve que la signature corrigée (*, callback_context, llm_request) fonctionne."""

    def test_truncate_callback_accepts_adk_kwargs_call(self):
        """create_truncate_history_callback retourne déjà un callback avec la
        bonne signature kwargs — il doit fonctionner sans TypeError."""
        mod = _get_callbacks()
        truncate_cb = mod.create_truncate_history_callback(keep_recent=5)

        fake_ctx = MagicMock()
        req = _mk_request(20)

        result = truncate_cb(callback_context=fake_ctx, llm_request=req)
        assert result is None

    def test_truncate_actually_truncates_via_adk_kwargs(self):
        """Après appel kwargs ADK, l'historique doit être réduit."""
        mod = _get_callbacks()
        truncate_cb = mod.create_truncate_history_callback(keep_recent=5)

        fake_ctx = MagicMock()
        req = _mk_request(20)
        assert len(req.contents) == 20

        truncate_cb(callback_context=fake_ctx, llm_request=req)

        assert len(req.contents) < 20, (
            f"truncate n'a pas réduit: {len(req.contents)} messages (attendu < 20)"
        )

    def test_strip_callback_accepts_adk_kwargs_call(self):
        """create_strip_large_payloads_callback a déjà la bonne signature kwargs."""
        mod = _get_history_compaction()
        strip_cb = mod.create_strip_large_payloads_callback("test_agent")

        fake_ctx = MagicMock()
        req = _mk_request(5)

        result = strip_cb(callback_context=fake_ctx, llm_request=req)
        assert result is None

    def test_fixed_chained_with_strip_accepts_adk_kwargs(self):
        """La VERSION CORRIGÉE de chained_with_strip (signature kwargs)
        doit fonctionner et exécuter truncate AVANT strip."""
        mod_cb = _get_callbacks()
        mod_hc = _get_history_compaction()

        truncate_cb = mod_cb.create_truncate_history_callback(keep_recent=5)
        strip_cb = mod_hc.create_strip_large_payloads_callback("test_agent")

        call_order: list[str] = []

        def _recording_truncate(*, callback_context, llm_request):
            call_order.append("truncate")
            return truncate_cb(callback_context=callback_context, llm_request=llm_request)

        def _recording_strip(*, callback_context, llm_request):
            call_order.append("strip")
            return strip_cb(callback_context=callback_context, llm_request=llm_request)

        # VERSION CORRIGÉE (ce que agent_utils doit avoir après le fix)
        _existing = _recording_truncate
        _strip = _recording_strip

        def chained_with_strip_FIXED(*, callback_context, llm_request,
                                     _strip_=_strip, _existing_=_existing):
            _existing_(callback_context=callback_context, llm_request=llm_request)
            _strip_(callback_context=callback_context, llm_request=llm_request)
            return None

        fake_ctx = MagicMock()
        req = _mk_request(20)

        result = chained_with_strip_FIXED(callback_context=fake_ctx, llm_request=req)

        assert result is None
        assert call_order == ["truncate", "strip"], (
            f"Ordre incorrect : {call_order} (attendu ['truncate', 'strip'])"
        )
        # truncate doit avoir réduit l'historique
        assert len(req.contents) < 20

    def test_fixed_chained_with_truncate_accepts_adk_kwargs(self):
        """La VERSION CORRIGÉE de chained_with_truncate (quand un before_model_cb
        existant est présent) passe les kwargs correctement aux sous-callbacks."""
        mod_cb = _get_callbacks()
        mod_hc = _get_history_compaction()

        truncate_cb = mod_cb.create_truncate_history_callback(keep_recent=5)
        strip_cb = mod_hc.create_strip_large_payloads_callback("test_agent")

        # existing_cb initial = strip_cb (simulant un callback préexistant)
        _ex = strip_cb
        _trunc = truncate_cb

        def chained_with_truncate_FIXED(*, callback_context, llm_request,
                                        _trunc_=_trunc, _ex_=_ex):
            _trunc_(callback_context=callback_context, llm_request=llm_request)
            return _ex_(callback_context=callback_context, llm_request=llm_request)

        fake_ctx = MagicMock()
        req = _mk_request(20)

        result = chained_with_truncate_FIXED(callback_context=fake_ctx, llm_request=req)
        assert result is None

    def test_fixed_truncate_only_accepts_adk_kwargs(self):
        """La VERSION CORRIGÉE de _truncate_only (branche sans before_model_cb initial)
        passe les kwargs correctement à truncate_cb."""
        mod = _get_callbacks()
        truncate_cb = mod.create_truncate_history_callback(keep_recent=5)

        def _truncate_only_FIXED(*, callback_context, llm_request, _trunc=truncate_cb):
            return _trunc(callback_context=callback_context, llm_request=llm_request)

        fake_ctx = MagicMock()
        req = _mk_request(20)

        result = _truncate_only_FIXED(callback_context=fake_ctx, llm_request=req)
        assert result is None

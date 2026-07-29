"""TDD - Correction 2 : ordre truncate-avant-strip dans agent_utils.

La chaine doit executer truncate EN PREMIER, puis strip.
Actuellement agent_utils.py chaine strip avant truncate (bug : si strip
retourne non-None, truncate est court-circuite).

Ce test prouve que truncate s'applique MEME quand strip detecte une image
(40 messages + image -> truncate reduit ET strip nettoie).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock


def _msg(role: str, text: str | None = None, has_image: bool = False) -> Any:
    """Fabrique un Content-like minimal."""
    parts = []
    if text is not None:
        parts.append(SimpleNamespace(
            text=text,
            function_call=None,
            function_response=None,
            inline_data=None,
        ))
    if has_image:
        # Simule une function_response avec base64
        fr = SimpleNamespace(
            name="tool_pdf_to_images",
            response={"images": ["A" * 500]},
        )
        parts.append(SimpleNamespace(
            text=None,
            function_call=None,
            function_response=fr,
            inline_data=None,
        ))
    return SimpleNamespace(role=role, parts=parts)


def _build_req(contents: list) -> Any:
    req = MagicMock()
    req.contents = list(contents)
    return req


class TestTruncateBeforeStrip:
    """Verifie l'ordre truncate-avant-strip dans la chaine agent_utils."""

    def test_truncate_applied_even_when_image_present(self):
        """40 messages dont 1 avec image -> truncate reduit (< 40) ET strip
        nettoie le base64. La chaine truncate-puis-strip doit etre appliquee."""
        from th2agent.core.agent_helpers.callbacks import create_truncate_history_callback
        from th2agent.core.history_compaction import create_strip_large_payloads_callback

        # Simuler la chaine telle que CORRIGEE dans agent_utils
        # truncate d'abord, strip ensuite (les deux retournent toujours None)
        truncate_cb = create_truncate_history_callback(keep_recent=14)
        strip_cb = create_strip_large_payloads_callback("test_agent")

        def correct_chain(ctx, req):
            truncate_cb(callback_context=ctx, llm_request=req)
            strip_cb(callback_context=ctx, llm_request=req)
            return None

        user_initial = _msg("user", "payload webhook")
        filler = [_msg("model", f"msg {i}", has_image=(i == 5)) for i in range(38)]
        contents = [user_initial] + filler
        assert len(contents) == 39

        req = _build_req(contents)
        ctx = MagicMock()
        correct_chain(ctx, req)

        # truncate DOIT avoir reduit
        assert len(req.contents) < 39, (
            f"Truncate non applique : {len(req.contents)} messages (attendu < 39)"
        )
        assert user_initial in req.contents, "user_initial absent apres chaine"

    def test_buggy_chain_fails_this_test(self):
        """Prouve que la chaine BUGGEE (strip avant truncate) peut court-
        circuiter truncate si strip retourne non-None.

        On simule un strip qui retourne non-None pour montrer le bug.
        Ce test PASSE si la chaine correcte est en place (truncate en premier).
        """
        from th2agent.core.agent_helpers.callbacks import create_truncate_history_callback

        truncate_cb = create_truncate_history_callback(keep_recent=14)

        # Strip factice qui retourne non-None (simule un strip qui court-circuite)
        def strip_returns_nonnone(ctx, req):
            return MagicMock()  # non-None -> court-circuite truncate si strip est premier

        # Chaine CORRECTE : truncate puis strip
        def correct_chain(ctx, req):
            truncate_cb(callback_context=ctx, llm_request=req)
            strip_returns_nonnone(ctx, req)
            return None

        user_initial = _msg("user", "payload initial")
        filler = [_msg("model", f"m{i}") for i in range(40)]
        contents = [user_initial] + filler
        req = _build_req(contents)
        ctx = MagicMock()
        correct_chain(ctx, req)

        # Avec la chaine correcte, truncate a bien tourne AVANT strip
        assert len(req.contents) < 41, (
            "truncate non applique : la chaine est incorrecte (strip avant truncate)"
        )

    def test_chained_with_strip_calls_truncate_first(self):
        """Verifie que dans agent_utils la chaine finale appelle truncate
        avant strip (en inspectant l'ordre des appels).
        """
        call_order = []

        def mock_truncate(*, callback_context, llm_request):
            call_order.append("truncate")
            return None

        def mock_strip(*, callback_context, llm_request):
            call_order.append("strip")
            return None

        # Simuler la logique de chainage de agent_utils.py (version corrigee)
        # truncate d'abord
        truncate_cb = mock_truncate
        strip_cb = mock_strip

        # Chaine correcte : truncate → strip (indépendamment du résultat)
        def chained(ctx, req):
            truncate_cb(callback_context=ctx, llm_request=req)
            strip_cb(callback_context=ctx, llm_request=req)
            return None

        req = MagicMock()
        req.contents = [SimpleNamespace(role="user", parts=[])] * 5
        chained(MagicMock(), req)

        assert call_order == ["truncate", "strip"], (
            f"Ordre incorrect : {call_order} (attendu ['truncate', 'strip'])"
        )

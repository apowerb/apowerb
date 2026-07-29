"""TDD - Levier 1 : troncature historique before_model_callback.

Tests ecrits AVANT l'implementation. Objectif : adapter le pipeline SCEI
au contexte 32k OVHcloud en tronquant l'historique ADK a keep_recent=14
messages + le message user initial.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock



# ---------------------------------------------------------------------------
# Helpers - construire des faux messages ADK sans importer google.adk
# ---------------------------------------------------------------------------

def _msg(role: str, text: str | None = None, tool_calls: int = 0, tool_name: str | None = None) -> Any:
    """Fabrique un objet Content-like minimal."""
    parts = []
    if text is not None:
        part = SimpleNamespace(text=text, function_call=None, function_response=None)
        parts.append(part)
    for i in range(tool_calls):
        fc = SimpleNamespace(name=f"tool_{i}", args={})
        parts.append(SimpleNamespace(text=None, function_call=fc, function_response=None))
    if tool_name is not None:
        fr = SimpleNamespace(name=tool_name, response={"rows": ["x"] * 200})
        parts.append(SimpleNamespace(text=None, function_call=None, function_response=fr))
    return SimpleNamespace(role=role, parts=parts)


def _build_llm_request(contents: list) -> Any:
    req = MagicMock()
    req.contents = contents
    return req


def _build_ctx() -> Any:
    return MagicMock()


# ---------------------------------------------------------------------------
# Import de la fonction a tester (echoue tant qu'elle n'existe pas)
# ---------------------------------------------------------------------------

def _import_cb():
    from th2agent.core.agent_helpers.callbacks import create_truncate_history_callback
    return create_truncate_history_callback


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestTruncateHistoryCallback:
    """Tests du callback de troncature historique."""

    def test_import_exists(self):
        """La fonction create_truncate_history_callback doit etre importable."""
        fn = _import_cb()
        assert callable(fn)

    def test_short_history_unchanged(self):
        """Si len(contents) <= keep_recent+1, retourne None (pas de troncature)."""
        create_cb = _import_cb()
        cb = create_cb(keep_recent=14)

        system = _msg("system", "tu es un agent SCEI")
        user = _msg("user", "traite cet AR")
        contents = [system, user]
        req = _build_llm_request(list(contents))
        result = cb(callback_context=_build_ctx(), llm_request=req)
        assert result is None
        assert req.contents == contents

    def test_exactly_at_limit_unchanged(self):
        """Exactement keep_recent+1 messages -> inchange."""
        create_cb = _import_cb()
        cb = create_cb(keep_recent=14)
        msgs = [_msg("system", "sys")] + [_msg("user", f"m{i}") for i in range(14)]
        req = _build_llm_request(list(msgs))
        result = cb(callback_context=_build_ctx(), llm_request=req)
        assert result is None

    def test_user_initial_always_present(self):
        """Le 1er message user (payload webhook) est toujours dans le resultat."""
        create_cb = _import_cb()
        cb = create_cb(keep_recent=14)

        system = _msg("system", "sys")
        user_initial = _msg("user", "payload webhook initial")
        tail = []
        for i in range(40):
            role = "model" if i % 2 == 0 else "tool"
            tail.append(_msg(role, f"msg {i}"))

        contents = [system, user_initial] + tail
        req = _build_llm_request(list(contents))
        cb(callback_context=_build_ctx(), llm_request=req)

        assert user_initial in req.contents

    def test_no_orphan_tool_at_window_head(self):
        """Un bloc assistant(4 tool_calls)+4*tool ne genere pas de tool orphelin en tete."""
        create_cb = _import_cb()
        cb = create_cb(keep_recent=14)

        system = _msg("system", "sys")
        user_initial = _msg("user", "declencheur")

        filler = []
        for _ in range(5):
            filler.append(_msg("model", "reflexion"))
            filler.append(_msg("tool", tool_name="tool_db"))

        assistant_with_4_calls = _msg("model", tool_calls=4)
        tool_responses = [_msg("tool", tool_name=f"t{i}") for i in range(4)]

        extra_after = [_msg("model", f"suite {i}") for i in range(10)]

        contents = [system, user_initial] + filler + [assistant_with_4_calls] + tool_responses + extra_after
        req = _build_llm_request(list(contents))
        cb(callback_context=_build_ctx(), llm_request=req)

        result = req.contents
        non_initial = [m for m in result if m.role not in ("system",) and m is not user_initial]
        if non_initial:
            assert non_initial[0].role != "tool", (
                f"Premier message de la fenetre est role='tool' (orphelin) : {non_initial[0]}"
            )

    def test_no_user_in_history_returns_none_with_warning(self, caplog):
        """Si aucun message role==user, retourne None + log warning (degradation gracieuse)."""
        create_cb = _import_cb()
        cb = create_cb(keep_recent=14)

        contents = [_msg("system", "sys")] + [_msg("model", f"m{i}") for i in range(30)]
        req = _build_llm_request(list(contents))
        original_contents = list(req.contents)

        with caplog.at_level(logging.WARNING, logger="th2agent.truncate_history"):
            result = cb(callback_context=_build_ctx(), llm_request=req)

        assert result is None
        assert req.contents == original_contents
        assert any("no user" in r.message.lower() or "user" in r.message.lower()
                   for r in caplog.records), "Aucun warning emis"

    def test_large_history_truncated_correctly(self):
        """40 messages avec gros tool_results -> resultat reduit, user initial present."""
        create_cb = _import_cb()
        cb = create_cb(keep_recent=14)

        system = _msg("system", "agent SCEI")
        user_initial = _msg("user", "AR a traiter : " + "x" * 500)

        big_rows = [{"col": "v" * 80, "idx": i} for i in range(150)]
        big_contents = []
        for i in range(19):
            model_msg = _msg("model", f"analyse {i}", tool_calls=1)
            tool_msg = SimpleNamespace(
                role="tool",
                parts=[SimpleNamespace(
                    text=None,
                    function_call=None,
                    function_response=SimpleNamespace(name="tool_db", response={"data": big_rows}),
                )]
            )
            big_contents.extend([model_msg, tool_msg])

        contents = [system, user_initial] + big_contents
        assert len(contents) == 40

        req = _build_llm_request(list(contents))
        cb(callback_context=_build_ctx(), llm_request=req)

        assert len(req.contents) < 40
        assert user_initial in req.contents
        assert len(req.contents) <= 14 + 2

    def test_assistant_tool_pairs_not_broken(self):
        """Un assistant avec tool_calls dans la fenetre garde ses tool_responses."""
        create_cb = _import_cb()
        cb = create_cb(keep_recent=14)

        system = _msg("system", "sys")
        user_initial = _msg("user", "depart")

        extra_filler = [_msg("model", f"extra {i}") for i in range(14)]
        assistant_block = _msg("model", tool_calls=2)
        tr1 = _msg("tool", tool_name="tool_a")
        tr2 = _msg("tool", tool_name="tool_b")
        final_model = _msg("model", "reponse finale")

        contents = [system, user_initial] + extra_filler + [assistant_block, tr1, tr2, final_model]
        assert len(contents) == 20

        req = _build_llm_request(list(contents))
        cb(callback_context=_build_ctx(), llm_request=req)

        result = req.contents
        assert assistant_block in result, (
            "assistant_block absent du resultat (regression : le bloc a ete orpheline)"
        )
        idx = result.index(assistant_block)
        assert tr1 in result, "tr1 manquant alors que assistant_block present"
        assert tr2 in result, "tr2 manquant alors que assistant_block present"
        assert result.index(tr1) > idx
        assert result.index(tr2) > idx

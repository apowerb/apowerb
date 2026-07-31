"""FIX 2 — TDD : reinjecter le TEXTE SOURCE du PDF dans le repair JSON.

Aujourd hui _attempt_json_repair ne recoit que le brouillon (raw) : il ne peut
pas re-extraire un champ absent du brouillon (ex. bon numero de commande).

Attendu :
1. _extract_pdf_source_text(callback_context) recupere le champ ``text`` du
   dernier function_response tool_pdf_first_page dans session.events.
2. _attempt_json_repair(..., source_text=...) inclut ce texte dans le prompt
   envoye a litellm.acompletion (tronque ~6000 chars).
3. Retro-compat : sans source_text, le prompt ne casse pas (pas de TEXTE SOURCE).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel


class _Schema(BaseModel):
    commande_number_sql: str


def _fr(name, response):
    return SimpleNamespace(name=name, response=response)


def _event(function_responses):
    ev = MagicMock()
    ev.get_function_responses.return_value = function_responses
    return ev


def _ctx_with_events(events):
    ctx = MagicMock()
    ctx._invocation_context.session.events = events
    return ctx


# --- 1. extraction du texte source ---------------------------------------

def test_extract_pdf_source_text_finds_latest():
    from apowerb.core.agent_helpers.callbacks import _extract_pdf_source_text
    events = [
        _event([_fr("tool_pdf_first_page", {"status": "success", "text": "VIEUX"})]),
        _event([_fr("other_tool", {"x": 1})]),
        _event([_fr("tool_pdf_first_page", {"status": "success", "text": "BON TEXTE CF100688"})]),
    ]
    ctx = _ctx_with_events(events)
    assert _extract_pdf_source_text(ctx) == "BON TEXTE CF100688"


def test_extract_pdf_source_text_none_when_absent():
    from apowerb.core.agent_helpers.callbacks import _extract_pdf_source_text
    ctx = _ctx_with_events([_event([_fr("other_tool", {"x": 1})])])
    assert _extract_pdf_source_text(ctx) is None


def test_extract_pdf_source_text_none_when_no_session():
    from apowerb.core.agent_helpers.callbacks import _extract_pdf_source_text
    ctx = MagicMock()
    # acceder a .events leve -> robustesse
    ctx._invocation_context.session.events = None
    assert _extract_pdf_source_text(ctx) is None


# --- 2. prompt repair inclut le texte source ------------------------------

@pytest.mark.asyncio
async def test_repair_prompt_includes_source_text():
    from apowerb.core.agent_helpers import callbacks
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = "{\"commande_number_sql\": \"100688\"}"
    with patch.object(callbacks.litellm, "acompletion", new=AsyncMock(return_value=resp)) as mock_ac:
        out = await callbacks._attempt_json_repair(
            _Schema,
            raw="{\"commande_number_sql\": \"CF100688/124212881\"}",
            repair_model="ovhcloud/x",
            source_text="VOICI LE TEXTE PDF avec CF100688 SCHNEIDER",
        )
    assert out is not None
    assert out.commande_number_sql == "100688"
    sent_prompt = mock_ac.call_args.kwargs["messages"][0]["content"]
    assert "VOICI LE TEXTE PDF avec CF100688 SCHNEIDER" in sent_prompt


@pytest.mark.asyncio
async def test_repair_prompt_truncates_source_text():
    from apowerb.core.agent_helpers import callbacks
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = "{\"commande_number_sql\": \"100688\"}"
    big = "A" * 9000
    with patch.object(callbacks.litellm, "acompletion", new=AsyncMock(return_value=resp)) as mock_ac:
        await callbacks._attempt_json_repair(
            _Schema, raw="{}", repair_model="ovhcloud/x", source_text=big,
        )
    sent_prompt = mock_ac.call_args.kwargs["messages"][0]["content"]
    assert "A" * 6000 in sent_prompt
    assert "A" * 6001 not in sent_prompt


@pytest.mark.asyncio
async def test_repair_prompt_omits_source_when_absent():
    from apowerb.core.agent_helpers import callbacks
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = "{\"commande_number_sql\": \"100688\"}"
    with patch.object(callbacks.litellm, "acompletion", new=AsyncMock(return_value=resp)) as mock_ac:
        out = await callbacks._attempt_json_repair(
            _Schema, raw="{}", repair_model="ovhcloud/x",
        )
    assert out is not None
    sent_prompt = mock_ac.call_args.kwargs["messages"][0]["content"]
    assert "TEXTE SOURCE" not in sent_prompt


# --- CORRECTION 2 — filtre status dans le repair-source -------------------
# Le repair ne doit PAS reinjecter le texte d un tool_pdf_first_page qui a
# echoue (status != "success"), sinon le modele re-extrait depuis un message
# d erreur. Ignorer l event en echec et chercher un success plus ancien.

def test_extract_ignores_failed_pdf_event():
    from apowerb.core.agent_helpers.callbacks import _extract_pdf_source_text
    events = [
        _event([_fr("tool_pdf_first_page", {"status": "error", "text": "ERREUR"})]),
    ]
    ctx = _ctx_with_events(events)
    assert _extract_pdf_source_text(ctx) is None


def test_extract_skips_recent_error_returns_older_success():
    from apowerb.core.agent_helpers.callbacks import _extract_pdf_source_text
    events = [
        _event([_fr("tool_pdf_first_page", {"status": "success", "text": "BON TEXTE CF100688"})]),
        _event([_fr("tool_pdf_first_page", {"status": "error", "text": "ERREUR RECENTE"})]),
    ]
    ctx = _ctx_with_events(events)
    assert _extract_pdf_source_text(ctx) == "BON TEXTE CF100688"

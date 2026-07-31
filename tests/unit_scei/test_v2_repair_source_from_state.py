"""TÂCHE 4 — TDD: _extract_pdf_source_text lit state['intake_pdf_text'] d abord.

Si le LLM n appelle plus tool_pdf_first_page (parce que le texte lui est
fourni par le gate), le repair-source doit recuperer la source depuis
state['intake_pdf_text']. Sinon, fallback sur les events tool_pdf_first_page.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock


def _fr(name, response):
    return SimpleNamespace(name=name, response=response)


def _event(function_responses):
    ev = MagicMock()
    ev.get_function_responses.return_value = function_responses
    return ev


def test_state_intake_pdf_text_takes_priority():
    from apowerb.core.agent_helpers.callbacks import _extract_pdf_source_text
    ctx = MagicMock()
    ctx.state = {"intake_pdf_text": "TEXTE GATE CF100688"}
    # meme si des events existent, le state prime
    ctx._invocation_context.session.events = [
        _event([_fr("tool_pdf_first_page", {"status": "success", "text": "EVENT TEXT"})]),
    ]
    assert _extract_pdf_source_text(ctx) == "TEXTE GATE CF100688"


def test_falls_back_to_events_when_state_absent():
    from apowerb.core.agent_helpers.callbacks import _extract_pdf_source_text
    ctx = MagicMock()
    ctx.state = {}
    ctx._invocation_context.session.events = [
        _event([_fr("tool_pdf_first_page", {"status": "success", "text": "EVENT TEXT CF1"})]),
    ]
    assert _extract_pdf_source_text(ctx) == "EVENT TEXT CF1"


def test_empty_state_value_falls_back_to_events():
    from apowerb.core.agent_helpers.callbacks import _extract_pdf_source_text
    ctx = MagicMock()
    ctx.state = {"intake_pdf_text": "   "}
    ctx._invocation_context.session.events = [
        _event([_fr("tool_pdf_first_page", {"status": "success", "text": "EVENT FALLBACK"})]),
    ]
    assert _extract_pdf_source_text(ctx) == "EVENT FALLBACK"


def test_state_access_error_does_not_break_fallback():
    from apowerb.core.agent_helpers.callbacks import _extract_pdf_source_text
    ctx = MagicMock()
    # .state leve -> on degrade vers events sans exception
    type(ctx).state = property(lambda self: (_ for _ in ()).throw(RuntimeError("no state")))
    ctx._invocation_context.session.events = [
        _event([_fr("tool_pdf_first_page", {"status": "success", "text": "EVENT SAFE"})]),
    ]
    assert _extract_pdf_source_text(ctx) == "EVENT SAFE"

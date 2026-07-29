"""Tests for the generic ``augment_agent_response`` extension point.

An overlay may append a DETERMINISTIC block to the displayed agent_response
(tool functionResponses never traverse extract_agent_response, and the LLM
cannot be trusted to echo them — cf. SCEI [RESEND_DIFF], incident 2026-07-15).
Contract: hook returns the augmented string (used as-is) or None/empty
(original kept); a hook failure NEVER breaks the run.
"""
import asyncio

import th2agent.routers.webhook_handlers.outlook as outlook_mod


def _run(coro):
    return asyncio.run(coro)


KW = dict(agent_id=12, user_id=1, session_id="webhook_1", log_id=1,
          response="texte LLM")


def _patch_hook(monkeypatch, hook):
    monkeypatch.setattr(
        outlook_mod._ext_registry, "webhook_hook",
        lambda point: hook if point == "augment_agent_response" else None,
    )


def test_no_hook_registered_keeps_response(monkeypatch):
    _patch_hook(monkeypatch, None)
    assert _run(outlook_mod._augment_agent_response(**KW)) == "texte LLM"


def test_hook_result_replaces_response(monkeypatch):
    async def hook(**kw):
        return kw["response"] + "\n\n[RESEND_DIFF] {}"
    _patch_hook(monkeypatch, hook)
    out = _run(outlook_mod._augment_agent_response(**KW))
    assert out.startswith("texte LLM")
    assert "[RESEND_DIFF]" in out


def test_hook_returning_none_keeps_response(monkeypatch):
    async def hook(**kw):
        return None
    _patch_hook(monkeypatch, hook)
    assert _run(outlook_mod._augment_agent_response(**KW)) == "texte LLM"


def test_hook_returning_empty_keeps_response(monkeypatch):
    async def hook(**kw):
        return ""
    _patch_hook(monkeypatch, hook)
    assert _run(outlook_mod._augment_agent_response(**KW)) == "texte LLM"


def test_hook_exception_keeps_response_and_never_raises(monkeypatch):
    async def hook(**kw):
        raise RuntimeError("overlay down")
    _patch_hook(monkeypatch, hook)
    assert _run(outlook_mod._augment_agent_response(**KW)) == "texte LLM"

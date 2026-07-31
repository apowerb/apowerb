"""Thinking Gemini reactive : reasoning_effort remplace thinking=disabled.

La desactivation historique (incident 03/06, PR #196 : les signatures
__thought__ cassaient l'appariement tool_call/tool_response) traitait le
symptome d'une regression google-adk 1.26.0 (#4650, corrigee >=1.27 : la
signature voyage dans Part.thought_signature). Avec adk>=1.36.2 le thinking
est rallume via reasoning_effort (borne a "low" par defaut, surchargable via
GEMINI_REASONING_EFFORT) — et thinking={"type":"disabled"} ne doit PLUS
apparaitre (il etait de toute facon partiellement placebo : thinkingConfig
vide sur gemini-2.5.x, thinkingLevel non nul force sur gemini-3.x).
"""
from apowerb.core.agent_helpers.llm_model_builder import build_litellm_model


def _details(model: str) -> dict:
    return {"agent_model": model, "agent_model_params": {}}


def test_gemini_reasoning_effort_low_by_default(monkeypatch):
    monkeypatch.delenv("GEMINI_REASONING_EFFORT", raising=False)
    m = build_litellm_model(_details("gemini/gemini-2.5-flash"), None)
    assert m._additional_args.get("reasoning_effort") == "low"


def test_gemini_thinking_disabled_gone(monkeypatch):
    monkeypatch.delenv("GEMINI_REASONING_EFFORT", raising=False)
    m = build_litellm_model(_details("gemini/gemini-3-flash-preview"), None)
    assert "thinking" not in m._additional_args


def test_gemini_reasoning_effort_env_kill_switch(monkeypatch):
    # Kill-switch sans redeploiement (efficace sur gemini-2.5.x uniquement)
    monkeypatch.setenv("GEMINI_REASONING_EFFORT", "none")
    m = build_litellm_model(_details("gemini/gemini-2.5-flash"), None)
    assert m._additional_args.get("reasoning_effort") == "none"


def test_non_gemini_untouched(monkeypatch):
    monkeypatch.delenv("GEMINI_REASONING_EFFORT", raising=False)
    m = build_litellm_model(
        _details("mistral/Mistral-Small-3.2-24B-Instruct-2506"), None
    )
    assert "reasoning_effort" not in m._additional_args
    assert "thinking" not in m._additional_args


def test_temperature_still_passed_alongside(monkeypatch):
    monkeypatch.delenv("GEMINI_REASONING_EFFORT", raising=False)
    m = build_litellm_model(_details("gemini/gemini-2.5-flash"), 0.3)
    assert m._additional_args.get("temperature") == 0.3
    assert m._additional_args.get("reasoning_effort") == "low"

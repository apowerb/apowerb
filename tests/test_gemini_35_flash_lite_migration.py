"""Migration gemini-2.5-flash -> gemini-3.5-flash-lite (deprecation Google).

Le 01/12/2026 Google redirige d'office ``gemini-2.5-flash`` vers
``gemini-3.5-flash-lite``. Sans action, litellm continuerait de construire une
requete au format 2.5 (thinkingBudget, temperature) pour un modele 3.x. On
renomme donc nous-memes, pour que la requete soit au format Gemini 3 et que la
grille tarifaire trouve la bonne ligne. L'alias est toujours applique.
"""
from pathlib import Path

import yaml

from apowerb.helpers.gemini_models import (
    drop_gemini3_sampling_params,
    effective_gemini_model,
)
from apowerb.core.agent_helpers.llm_model_builder import build_litellm_model

LITE = "gemini/gemini-3.5-flash-lite"


def _details(model: str) -> dict:
    return {"agent_model": model, "agent_model_params": {}}


def test_alias_rewrites_deprecated_flash():
    assert effective_gemini_model("gemini/gemini-2.5-flash") == LITE
    assert effective_gemini_model("gemini-2.5-flash") == "gemini-3.5-flash-lite"


def test_alias_leaves_other_models():
    for m in ("gemini/gemini-2.5-flash-lite", "gemini/gemini-2.5-pro",
              "gemini-2.5-flash-native-audio-preview-12-2025", "openai/gpt-6-luna", ""):
        assert effective_gemini_model(m) == m


def test_alias_cannot_be_switched_off(monkeypatch):
    # L'ancien kill-switch GEMINI_25_FLASH_ALIAS=off n'existe plus : gemini-2.5-flash est arrete.
    monkeypatch.setenv("GEMINI_25_FLASH_ALIAS", "off")
    assert effective_gemini_model("gemini/gemini-2.5-flash") == LITE
    assert effective_gemini_model("gemini-2.5-flash") == "gemini-3.5-flash-lite"


def test_builder_uses_lite_with_medium_thinking(monkeypatch):
    # Guide Google : medium/high pour les agents a outils, sinon arret premature.
    monkeypatch.delenv("GEMINI_REASONING_EFFORT", raising=False)
    m = build_litellm_model(_details("gemini/gemini-2.5-flash"), 0.3)
    assert m.model == LITE
    assert m._additional_args.get("reasoning_effort") == "medium"
    assert "temperature" not in m._additional_args


def test_builder_gemini3_reasoning_env_override(monkeypatch):
    monkeypatch.setenv("GEMINI_REASONING_EFFORT", "high")
    m = build_litellm_model(_details(LITE), None)
    assert m._additional_args.get("reasoning_effort") == "high"


def test_sampling_params_dropped_for_gemini3_only():
    kw = {"model": "gemini/gemini-2.5-flash", "temperature": 0.0, "top_p": 0.9,
          "top_k": 5, "candidate_count": 1, "messages": []}
    out = drop_gemini3_sampling_params(kw)
    assert out["model"] == LITE
    assert not {"temperature", "top_p", "top_k", "candidate_count"} & out.keys()

    for model in ("openai/gpt-6-luna", "gemini-2.5-flash-native-audio-preview-12-2025"):
        kept = drop_gemini3_sampling_params({"model": model, "temperature": 0.2})
        assert kept == {"model": model, "temperature": 0.2}


def test_pricing_has_lite_at_25_flash_rates():
    p = Path(__file__).resolve().parents[1] / "src/apowerb/configs/model_pricing.yaml"
    grid = yaml.safe_load(p.read_text())
    models = grid.get("models", grid)
    assert models[LITE] == models["gemini/gemini-2.5-flash"]

"""Estimated cost of LLM usage, from the versioned pricing grid.

The grid lives in ``th2agent/configs/model_pricing.yaml`` (prices per
1M tokens, currency EUR). A model absent from the grid yields ``None`` --
never ``0.0`` -- so the UI can render "—" instead of claiming a free call.
Distinguishing "unknown" from "free" is the whole point: OVH models are
priced at an explicit 0.0 in the grid.
"""

from __future__ import annotations

import threading
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml

from apowerb.configs.th2logger import setup_logging

logger = setup_logging(__name__)

_PRICING_PATH = Path(__file__).resolve().parent.parent / "configs" / "model_pricing.yaml"

_PER_MILLION = 1_000_000

_lock = threading.Lock()


@lru_cache(maxsize=1)
def load_pricing() -> dict:
    """Parse the pricing grid once per process. A missing or malformed
    file degrades to an empty grid (every model then costs ``None``) --
    a pricing problem must never break the usage endpoints.
    """
    try:
        with _PRICING_PATH.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        models = data.get("models") or {}
        if not isinstance(models, dict):
            raise ValueError("'models' must be a mapping")
        return models
    except Exception as exc:
        logger.warning("[MODEL_PRICING] could not load %s: %s", _PRICING_PATH, exc)
        return {}


def estimate_cost_eur(
    model: Optional[str],
    input_tokens: int = 0,
    output_tokens: int = 0,
    thoughts_tokens: int = 0,
    cached_tokens: int = 0,
) -> Optional[float]:
    """Estimated EUR cost of one (or an aggregate of) model call(s).

    Two accounting subtleties, both of which silently under-count if
    ignored:

    * ``thoughts_tokens`` (Gemini 2.5+ reasoning) are billed as OUTPUT by
      the provider, so they are added to ``output_tokens`` here.
    * ``cached_tokens`` are a SUBSET of ``input_tokens`` already counted
      by the provider's ``prompt_token_count``. They are billed at the
      reduced ``cached_input`` rate, so the full-rate share is
      ``input_tokens - cached_tokens``.

    Returns ``None`` when the model has no entry in the grid.
    """
    if not model:
        return None

    rates = load_pricing().get(model)
    if not isinstance(rates, dict):
        return None

    try:
        full_rate_input = max((input_tokens or 0) - (cached_tokens or 0), 0)
        billable_output = (output_tokens or 0) + (thoughts_tokens or 0)

        cost = (
            full_rate_input * float(rates.get("input", 0.0))
            + (cached_tokens or 0) * float(rates.get("cached_input", 0.0))
            + billable_output * float(rates.get("output", 0.0))
        ) / _PER_MILLION
        return round(cost, 6)
    except (TypeError, ValueError) as exc:
        logger.warning("[MODEL_PRICING] malformed rates for model %r: %s", model, exc)
        return None


def sum_costs(costs: list[Optional[float]]) -> Optional[float]:
    """Total of per-model costs, ignoring the unpriced ones.

    Returns ``None`` only when NOTHING was priced -- a partial total would
    otherwise be indistinguishable from a complete one. When at least one
    model is priced the total is returned, deliberately under-stating the
    real cost; the UI shows which models are unpriced alongside it.
    """
    known = [c for c in costs if c is not None]
    if not known:
        return None
    return round(sum(known), 6)

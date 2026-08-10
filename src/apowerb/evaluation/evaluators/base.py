"""Shared types for agent evaluators.

An evaluator scores one dimension of one target (a session, an invocation)
and returns an :class:`EvaluationOutcome`. Deterministic evaluators never
call an LLM; judge evaluators do, and must use a model different from the
one that produced the conversation being scored (self-preference bias --
see ``task_completion_judge.py``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

EvaluatorKind = Literal["deterministic", "llm_judge"]


@dataclass
class EvaluationOutcome:
    """Result of running one evaluator against one target.

    ``score`` is always normalized to [0.0, 1.0], higher is better, so
    results from different evaluators can be compared or aggregated
    without each caller re-deriving a scale.
    """

    evaluator: str
    kind: EvaluatorKind
    score: float
    passed: bool
    details: dict[str, Any] = field(default_factory=dict)

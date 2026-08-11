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

    ``score`` is normalized to [0.0, 1.0], higher is better, so results from
    different evaluators can be compared or aggregated without each caller
    re-deriving a scale.

    ``score`` and ``passed`` are ``None`` when the evaluator could not judge
    at all — no telemetry for the session, no tool call to score. That is a
    third state, and it must never be flattened into 0.0: averaged together,
    a session nobody instrumented would drag a dashboard down exactly like an
    agent that failed every single call, and no reader could tell them apart.
    ``None`` was chosen over a boolean flag on purpose — a flag can be
    forgotten, while ``sum(o.score for o in outcomes)`` raises on ``None``.
    """

    evaluator: str
    kind: EvaluatorKind
    score: float | None
    passed: bool | None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def applicable(self) -> bool:
        """False when this evaluator had nothing to judge."""
        return self.score is not None

    @classmethod
    def not_applicable(
        cls,
        *,
        evaluator: str,
        kind: EvaluatorKind,
        reason: str,
        **details: Any,
    ) -> "EvaluationOutcome":
        """An outcome that carries why it could not be produced."""
        return cls(
            evaluator=evaluator,
            kind=kind,
            score=None,
            passed=None,
            details={"not_applicable": reason, **details},
        )


_RATIONALE_LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "fr": "French",
    "es": "Spanish",
    "de": "German",
    "it": "Italian",
    "pt": "Portuguese",
}


def rationale_language(locale: str | None) -> str:
    """Human-readable language name for a `rationale` prompt, from a locale.

    `rationale` addresses the person reading the screen, not the
    conversation being judged -- it follows the interface's locale, never
    the judged session's own language. Unknown codes fall back to the raw
    string: still a reasonable instruction for the model, and a system this
    permissive should not have to be extended for every new UI locale.
    Empty/missing locale defaults to English, matching `POST /run`'s own
    default.
    """
    code = (locale or "en").strip().lower()
    return _RATIONALE_LANGUAGE_NAMES.get(code, code)

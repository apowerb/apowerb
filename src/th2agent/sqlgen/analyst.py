"""Pure Business-Analyst investigation + interpretation loop.

This is the orchestration the text-to-SQL layer was missing: instead of one
question -> one query, an *investigation* runs several chained queries, then a
single interpretation pass turns the gathered evidence into a narrative,
findings, recommendations and an optional chart spec.

It stays PURE on purpose (no ADK, no DB driver, no LLM client, no settings):
the three moving parts are injected as callables so the whole loop is unit
-testable with fakes. The live wiring (Mistral-Large planner/interpreter +
psycopg2 executor) lives in tools_store/portfolio/text_to_sql.py.

    plan_next(question, schema_info, steps) -> dict
        Decides the next move. Returns either {"done": True} to stop, or
        {"sub_question": str, "sql": str} for the next query to run. It sees
        every prior InvestigationStep so it can build on earlier results.

    execute(sql) -> list[dict]
        Runs a validated SELECT and returns rows. May raise; the loop catches.

    interpret(question, steps) -> dict
        Turns the gathered steps into
        {"narrative", "findings", "recommendations", "chart"}.

Safety is non-negotiable and independent of the model: every SQL string is run
through validate_sql_safety before execution, so a hallucinated DROP/DELETE is
recorded as a failed step and NEVER executed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from th2agent.sqlgen.generator import extract_sql
from th2agent.sqlgen.safety import validate_sql_safety

logger = logging.getLogger(__name__)

# A planner that keeps proposing unsafe SQL is malfunctioning; stop after this
# many consecutive rejections rather than burning the whole step budget on it.
_MAX_CONSECUTIVE_UNSAFE = 2

PlanNext = Callable[[str, Dict[str, Any], List["InvestigationStep"]], Dict[str, Any]]
Execute = Callable[[str], List[Dict[str, Any]]]
Interpret = Callable[[str, List["InvestigationStep"]], Dict[str, Any]]
Validate = Callable[[str], Tuple[bool, Optional[str]]]


@dataclass
class InvestigationStep:
    """One query in the investigation: the sub-question, its SQL and outcome."""

    sub_question: str
    sql: str
    rows: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class AnalysisResult:
    """Outcome of a full investigation + interpretation."""

    question: str
    steps: List[InvestigationStep] = field(default_factory=list)
    narrative: str = ""
    findings: List[str] = field(default_factory=list)
    recommendations: List[str] = field(default_factory=list)
    chart: Optional[Dict[str, Any]] = None

    @property
    def ok(self) -> bool:
        """True when at least one step returned data without error."""
        return any(s.ok and s.row_count > 0 for s in self.steps)


def run_investigation(
    question: str,
    schema_info: Dict[str, Any],
    *,
    plan_next: PlanNext,
    execute: Execute,
    interpret: Interpret,
    max_steps: int = 4,
    validate: Validate = validate_sql_safety,
) -> AnalysisResult:
    """Run a bounded investigation then interpret the evidence.

    The loop asks ``plan_next`` for the next query, validates and executes it,
    feeding every prior step back into the planner so it can chain. It stops on
    ``{"done": True}``, when the planner stops proposing SQL, or after
    ``max_steps`` queries -- whichever comes first. ``interpret`` is only called
    when there is at least one step to reason about; an empty investigation
    returns a graceful, model-free result.
    """
    steps: List[InvestigationStep] = []
    consecutive_unsafe = 0

    for _ in range(max_steps):
        plan = plan_next(question, schema_info, steps) or {}
        if plan.get("done"):
            break

        sql = extract_sql(plan.get("sql") or "")
        if not sql:
            # Planner proposed nothing actionable and did not say done:
            # stop rather than spin uselessly until max_steps.
            break

        sub_q = plan.get("sub_question") or question
        is_safe, err = validate(sql)
        if not is_safe:
            logger.warning("[ANALYST] unsafe SQL rejected (%s): %s", err, sql)
            steps.append(InvestigationStep(
                sub_question=sub_q, sql=sql,
                error=f"unsafe SQL rejected: {err}"))
            consecutive_unsafe += 1
            if consecutive_unsafe >= _MAX_CONSECUTIVE_UNSAFE:
                logger.warning("[ANALYST] aborting: %d consecutive unsafe plans",
                               consecutive_unsafe)
                break
            continue

        consecutive_unsafe = 0
        try:
            rows = execute(sql)
            steps.append(InvestigationStep(
                sub_question=sub_q, sql=sql, rows=list(rows or [])))
        except Exception as exc:  # executor failure -> recorded, loop continues
            steps.append(InvestigationStep(
                sub_question=sub_q, sql=sql, error=str(exc)))

    if not steps:
        return AnalysisResult(
            question=question,
            narrative="Aucune requete exploitable n'a pu etre formulee.",
        )

    # No step executed successfully (all unsafe / all errored): skip the
    # interpretation LLM call entirely -- there is nothing to interpret.
    if not any(s.ok for s in steps):
        return AnalysisResult(
            question=question, steps=steps,
            narrative="L'investigation n'a produit aucune requete executable.",
        )

    parsed = interpret(question, steps) or {}
    return AnalysisResult(
        question=question,
        steps=steps,
        narrative=parsed.get("narrative", ""),
        findings=list(parsed.get("findings") or []),
        recommendations=list(parsed.get("recommendations") or []),
        chart=parsed.get("chart"),
    )

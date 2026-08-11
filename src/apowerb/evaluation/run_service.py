"""Executes the existing evaluators against a session and persists results.

Owns the two decisions the HTTP layer needs and the evaluators themselves
don't make:

- Which session belongs to which agent/owner -- checked once, at the
  entry of the request (`resolve_session_context`, `owned_agent_ids`),
  never by filtering an already-built list. Filtering a fetched list is
  the BOLA shape already found once on this product's Logging page.
- How a judge failure folds into a non-applicable result instead of a
  500 that would also take the deterministic evaluator's result down
  with it (`_run_judge`).

`details` on every persisted row is exactly what the evaluator returned --
no renaming, no reshaping. The front is built against that exact shape
(`per_tool` for the deterministic evaluator, `rationale` /
`task_completion` / `intent_resolution` / `turns` /
`judge_shares_provider_with_judged` for the judge).

`KNOWN_EVALUATORS` also lists `tool_usage` (deterministic, `pulse_spans`),
`coherence` / `completeness` (LLM judges, `_run_llm_judge` generalizes the
never-raises guarantee `_run_judge` already had for
`task_completion_judge`), and `hallucination` (LLM judge, degraded --
`details["grounding"] = "unavailable"`, see its module docstring).
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import quoted_name

from apowerb.configs.settings import get_settings
from apowerb.evaluation.evaluators.base import EvaluationOutcome
from apowerb.evaluation.evaluators.coherence import evaluate_coherence
from apowerb.evaluation.evaluators.completeness import evaluate_completeness
from apowerb.evaluation.evaluators.hallucination import evaluate_hallucination
from apowerb.evaluation.evaluators.task_completion_judge import (
    SameJudgeError,
    evaluate_task_completion,
)
from apowerb.evaluation.evaluators.tool_execution_outcome import (
    evaluate_tool_execution_outcome,
)
from apowerb.evaluation.evaluators.tool_usage import evaluate_tool_usage
from apowerb.evaluation.models import EvaluationResult
from apowerb.users import schemas as user_schemas

logger = logging.getLogger(__name__)

KNOWN_EVALUATORS = (
    "tool_execution_outcome",
    "task_completion_judge",
    "tool_usage",
    "coherence",
    "completeness",
    "hallucination",
)

# Same convention as helpers/ownership.py's validate_agent_ownership: an
# agent's app_name is "agent<numeric id>". Superagents and other app_name
# shapes are out of scope for v1 -- see the dev report.
_AGENT_APP_NAME_RE = re.compile(r"^agent(\d+)$")


def _schema() -> str:
    return str(quoted_name(get_settings().db_schema, quote=True))


@dataclass
class SessionContext:
    agent_id: int
    app_name: str
    session_user_id: str
    judged_model: str | None
    owner_id: str


async def resolve_session_context(
    db: AsyncSession, session_id: str, current_user: user_schemas.User
) -> SessionContext:
    """Resolve agent_id/app_name/user_id/judged_model from a bare
    session_id and enforce ownership before returning anything -- an admin
    bypasses, everyone else must own the agent behind this session.
    """
    row = (
        await db.execute(
            text(
                f"SELECT app_name, user_id FROM {_schema()}.sessions "
                "WHERE id = :session_id LIMIT 1"
            ),
            {"session_id": session_id},
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Session not found")

    app_name, session_user_id = row
    match = _AGENT_APP_NAME_RE.match(app_name or "")
    if not match:
        raise HTTPException(
            status_code=404, detail="Session's agent could not be resolved"
        )
    agent_id = int(match.group(1))

    from apowerb.core.agent_main import agent_store

    select_query = agent_store.agent_table.select().where(
        agent_store.agent_table.c.agent_id == agent_id
    )
    rows = await asyncio.to_thread(agent_store.get_list_agents, select_query)
    if not rows:
        raise HTTPException(status_code=404, detail="Agent not found")

    agent = rows[0]._asdict()
    owner_id = str(agent.get("owner_id"))
    if current_user.role != "admin" and owner_id != str(current_user.email):
        logger.warning(
            "[EVAL] Denied: user %s tried to evaluate session %s of agent %s (owner=%s)",
            current_user.email, session_id, agent_id, owner_id,
        )
        raise HTTPException(status_code=403, detail="Not your agent")

    return SessionContext(
        agent_id=agent_id,
        app_name=app_name,
        session_user_id=session_user_id,
        judged_model=agent.get("agent_model"),
        owner_id=owner_id,
    )


async def owned_agent_ids(db: AsyncSession, current_user: user_schemas.User) -> set[int] | None:
    """None means unrestricted (admin). Otherwise the exact set of
    agent_ids this user owns -- callers must apply it when building the
    query, never after fetching rows.
    """
    if current_user.role == "admin":
        return None

    from apowerb.core.agent_main import agent_store

    select_query = (
        agent_store.agent_table.select()
        .where(agent_store.agent_table.c.owner_id == current_user.email)
        .with_only_columns(agent_store.agent_table.c.agent_id)
    )
    rows = await asyncio.to_thread(agent_store.get_list_agents, select_query)
    return {row[0] for row in rows}


async def _run_judge(
    db: AsyncSession, ctx: SessionContext, session_id: str
) -> EvaluationOutcome:
    """Never raises: not configured, same-judge-as-judged, or any other
    failure (litellm timeout, malformed response, ...) all become a
    non-applicable outcome carrying the reason, so a judge problem can
    never take the deterministic evaluator's result down with it.
    """
    try:
        return await evaluate_task_completion(
            db,
            app_name=ctx.app_name,
            user_id=ctx.session_user_id,
            session_id=session_id,
            judged_model=ctx.judged_model or "",
        )
    except (SameJudgeError, RuntimeError) as exc:
        return EvaluationOutcome.not_applicable(
            evaluator="task_completion_judge",
            kind="llm_judge",
            reason=str(exc),
            session_id=session_id,
        )
    except Exception as exc:  # noqa: BLE001 -- judge must never fail the run
        logger.warning(
            "[EVAL] Judge evaluator failed for session %s: %s", session_id, exc
        )
        return EvaluationOutcome.not_applicable(
            evaluator="task_completion_judge",
            kind="llm_judge",
            reason=f"judge evaluation failed: {exc}",
            session_id=session_id,
        )


async def _run_llm_judge(
    evaluator_name: str,
    judge_fn,
    db: AsyncSession,
    ctx: SessionContext,
    session_id: str,
) -> EvaluationOutcome:
    """Same never-raises guarantee as `_run_judge`, generalized to the
    coherence/completeness/hallucination judges: not configured,
    same-judge-as-judged, or any other failure all become a non-applicable
    outcome instead of an exception, so one judge problem can never take
    another evaluator's already-computed result down with it.
    """
    try:
        return await judge_fn(
            db,
            app_name=ctx.app_name,
            user_id=ctx.session_user_id,
            session_id=session_id,
            judged_model=ctx.judged_model or "",
        )
    except (SameJudgeError, RuntimeError) as exc:
        return EvaluationOutcome.not_applicable(
            evaluator=evaluator_name,
            kind="llm_judge",
            reason=str(exc),
            session_id=session_id,
        )
    except Exception as exc:  # noqa: BLE001 -- judge must never fail the run
        logger.warning(
            "[EVAL] Judge evaluator %s failed for session %s: %s",
            evaluator_name,
            session_id,
            exc,
        )
        return EvaluationOutcome.not_applicable(
            evaluator=evaluator_name,
            kind="llm_judge",
            reason=f"judge evaluation failed: {exc}",
            session_id=session_id,
        )


async def run_and_persist(
    db: AsyncSession,
    ctx: SessionContext,
    session_id: str,
    evaluators: list[str] | None,
) -> list[EvaluationResult]:
    names = list(evaluators) if evaluators is not None else list(KNOWN_EVALUATORS)
    unknown = [name for name in names if name not in KNOWN_EVALUATORS]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown evaluator(s): {unknown}")

    settings = get_settings()
    outcomes: list[EvaluationOutcome] = []
    if "tool_execution_outcome" in names:
        outcomes.append(await evaluate_tool_execution_outcome(db, session_id))
    if "task_completion_judge" in names:
        outcomes.append(await _run_judge(db, ctx, session_id))
    if "tool_usage" in names:
        outcomes.append(await evaluate_tool_usage(db, session_id))
    if "coherence" in names:
        outcomes.append(
            await _run_llm_judge("coherence", evaluate_coherence, db, ctx, session_id)
        )
    if "completeness" in names:
        outcomes.append(
            await _run_llm_judge(
                "completeness", evaluate_completeness, db, ctx, session_id
            )
        )
    if "hallucination" in names:
        outcomes.append(
            await _run_llm_judge(
                "hallucination", evaluate_hallucination, db, ctx, session_id
            )
        )

    rows: list[EvaluationResult] = []
    for outcome in outcomes:
        row = EvaluationResult(
            agent_id=ctx.agent_id,
            session_id=session_id,
            invocation_id=None,
            evaluator_name=outcome.evaluator,
            evaluator_kind=outcome.kind,
            judge_model=(settings.evaluation_judge_model or None) if outcome.kind == "llm_judge" else None,
            score=outcome.score,
            passed=outcome.passed,
            details=outcome.details,
        )
        db.add(row)
        rows.append(row)

    await db.commit()
    for row in rows:
        await db.refresh(row)
    return rows

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
`judge_shares_provider_with_judged` / `judge_is_byom` for the judge).

`EVALUATOR_REGISTRY` is the single source of truth for what evaluators
exist: `GET /api/evaluations/evaluators` (routers/evaluations.py) reads it
to let the front build its checkboxes without a hardcoded list, and
`run_and_persist` reads it to validate incoming evaluator names. Adding an
evaluator is an entry here, not a front change.

Beyond `tool_execution_outcome` and `task_completion_judge` it carries
`tool_usage` (deterministic, `pulse_spans`), `coherence` / `completeness`
(LLM judges) and `hallucination` (LLM judge, degraded --
`details["grounding"] = "unavailable"`, see its module docstring).
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
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
from apowerb.models import LlmUsage
from apowerb.users import schemas as user_schemas

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EvaluatorSpec:
    """Metadata for one evaluator, as exposed by `GET /evaluations/evaluators`."""

    name: str
    kind: str  # "deterministic" | "llm_judge"
    requires_judge: bool


EVALUATOR_REGISTRY: tuple[EvaluatorSpec, ...] = (
    EvaluatorSpec(name="tool_execution_outcome", kind="deterministic", requires_judge=False),
    EvaluatorSpec(name="task_completion_judge", kind="llm_judge", requires_judge=True),
    EvaluatorSpec(name="tool_usage", kind="deterministic", requires_judge=False),
    EvaluatorSpec(name="coherence", kind="llm_judge", requires_judge=True),
    EvaluatorSpec(name="completeness", kind="llm_judge", requires_judge=True),
    EvaluatorSpec(name="hallucination", kind="llm_judge", requires_judge=True),
)

KNOWN_EVALUATORS = tuple(spec.name for spec in EVALUATOR_REGISTRY)


def list_evaluator_specs() -> tuple[EvaluatorSpec, ...]:
    return EVALUATOR_REGISTRY


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
    # Business name of the agent ("Analyste AR"), not the ADK appName
    # ("agent1234") -- used to label `llm_usage` rows the same way an
    # agent turn does. Falls back to `app_name` when the store row carries
    # no name.
    agent_name: str = ""


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
        agent_name=agent.get("agent_name") or app_name,
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
    db: AsyncSession,
    ctx: SessionContext,
    session_id: str,
    *,
    judge_model: str | None = None,
    judge_api_key: str | None = None,
    locale: str | None = None,
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
            judge_model=judge_model,
            judge_api_key=judge_api_key,
            locale=locale,
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


async def _record_judge_usage(
    db: AsyncSession, ctx: SessionContext, session_id: str, outcome: EvaluationOutcome
) -> None:
    """Write one `llm_usage` row for a judge call, the same way an agent
    turn is recorded -- `invocation_source="evaluation"` is what separates
    it from a chat turn. `billed_to_thaink2` is `True` when the server's
    shared judge served the call, `False` in BYOM (the caller's own key
    paid for it). This split is a default proposed in the dev report, not
    yet confirmed by David/Farid.

    Best-effort: an accounting failure must never fail the evaluation, but
    it is logged loudly (not swallowed) so it cannot vanish silently.
    """
    usage = outcome.details.get("judge_usage")
    judge_model = outcome.details.get("judge_model")
    if not usage or not judge_model:
        return

    try:
        db.add(
            LlmUsage(
                agent_id=ctx.agent_id,
                agent_name=ctx.agent_name or ctx.app_name,
                owner_id=ctx.owner_id,
                session_id=session_id,
                invocation_id=None,
                invocation_source="evaluation",
                model=judge_model,
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                thoughts_tokens=usage.get("thoughts_tokens", 0),
                cached_tokens=usage.get("cached_tokens", 0),
                total_tokens=usage.get("total_tokens", 0),
                tool_names=None,
                billed_to_thaink2=not bool(outcome.details.get("judge_is_byom")),
            )
        )
        await db.commit()
    except Exception as exc:  # noqa: BLE001 -- accounting must never fail the evaluation
        logger.error(
            "[EVAL] Failed to record judge llm_usage for session %s (model=%s): %s",
            session_id, judge_model, exc,
        )
        await db.rollback()


async def _run_llm_judge(
    evaluator_name: str,
    judge_fn,
    db: AsyncSession,
    ctx: SessionContext,
    session_id: str,
    *,
    judge_model: str | None = None,
    judge_api_key: str | None = None,
    locale: str | None = None,
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
            # Without this, a caller's own model would be honoured by
            # task_completion_judge and silently ignored by these three.
            judge_model=judge_model,
            judge_api_key=judge_api_key,
            locale=locale,
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
    *,
    judge_model: str | None = None,
    judge_api_key: str | None = None,
    run_id: uuid.UUID | None = None,
    locale: str | None = None,
) -> list[EvaluationResult]:
    # One id shared by every result of this call -- the router generates it
    # up front (it belongs at the root of the HTTP response even when the
    # evaluator list is empty) and passes it in; a direct caller that omits
    # it still gets one, so no row is ever persisted without a run_id.
    run_id = run_id or uuid.uuid4()
    names = list(evaluators) if evaluators is not None else list(KNOWN_EVALUATORS)
    unknown = [name for name in names if name not in KNOWN_EVALUATORS]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown evaluator(s): {unknown}")

    outcomes: list[EvaluationOutcome] = []
    if "tool_execution_outcome" in names:
        outcomes.append(await evaluate_tool_execution_outcome(db, session_id))
    if "task_completion_judge" in names:
        outcomes.append(
            await _run_judge(
                db, ctx, session_id,
                judge_model=judge_model, judge_api_key=judge_api_key,
                locale=locale,
            )
        )
    if "tool_usage" in names:
        outcomes.append(await evaluate_tool_usage(db, session_id))
    if "coherence" in names:
        outcomes.append(
            await _run_llm_judge(
                "coherence", evaluate_coherence, db, ctx, session_id,
                judge_model=judge_model, judge_api_key=judge_api_key,
                locale=locale,
            )
        )
    if "completeness" in names:
        outcomes.append(
            await _run_llm_judge(
                "completeness", evaluate_completeness, db, ctx, session_id,
                judge_model=judge_model, judge_api_key=judge_api_key,
                locale=locale,
            )
        )
    if "hallucination" in names:
        outcomes.append(
            await _run_llm_judge(
                "hallucination", evaluate_hallucination, db, ctx, session_id,
                judge_model=judge_model, judge_api_key=judge_api_key,
                locale=locale,
            )
        )

    rows: list[EvaluationResult] = []
    for outcome in outcomes:
        row = EvaluationResult(
            agent_id=ctx.agent_id,
            run_id=run_id,
            session_id=session_id,
            invocation_id=None,
            evaluator_name=outcome.evaluator,
            evaluator_kind=outcome.kind,
            # The model EFFECTIVELY used by the judge (may be BYOM), read
            # from the outcome's own details -- not the server's settings,
            # which the caller may have overridden for this run.
            judge_model=outcome.details.get("judge_model") if outcome.kind == "llm_judge" else None,
            score=outcome.score,
            passed=outcome.passed,
            details=outcome.details,
        )
        db.add(row)
        rows.append(row)

    await db.commit()

    # Usage accounting's own commit runs BEFORE the refresh below, not
    # after: SQLAlchemy expires every object in the session on commit
    # (the default `expire_on_commit=True`), including these rows that
    # were just persisted. Refreshing first and writing usage second
    # would hand the router back expired rows -- `EvaluationResultOut`
    # reading `row.id` outside an awaited context then crashes with
    # `MissingGreenlet`. Only reproduces against a real AsyncSession;
    # mocks don't model expiration.
    for outcome in outcomes:
        if outcome.kind == "llm_judge":
            await _record_judge_usage(db, ctx, session_id, outcome)

    for row in rows:
        await db.refresh(row)

    return rows

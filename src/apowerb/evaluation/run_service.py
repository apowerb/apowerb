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
import time
import uuid
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import quoted_name

from apowerb.configs.settings import get_settings
from apowerb.evaluation.evaluators._shared_judge import fetch_transcript
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
from apowerb.models import LlmUsage, UserRole
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
    # "llm_usage" when `judged_model` came from the model that actually
    # produced this session; "agent_config_fallback" when no usable
    # `llm_usage` row existed and it fell back to the agent's CURRENT
    # config instead -- which may not be what produced this conversation
    # (agents change model over time). Threaded into every judge outcome's
    # `details["judged_model_source"]` so a score built on the fallback
    # never passes for one built on fact.
    judged_model_source: str = "llm_usage"


def _judged_model_sql():
    # Excludes this evaluator's OWN rows: `_record_judge_usage` writes
    # `llm_usage` rows for the SAME session_id (invocation_source=
    # "evaluation") once a judge has run on it once. Without this filter,
    # a second evaluation run would pick up the judge's own model as if it
    # were the judged model -- verified against real DEV data
    # (session_1786432883708: the judge's gemini-2.5-pro rows sort after
    # the chat's gemini-3-flash-preview rows by created_at).
    return text(
        f"SELECT model FROM {_schema()}.llm_usage "
        "WHERE session_id = :session_id "
        "AND (invocation_source IS NULL OR invocation_source <> 'evaluation') "
        "ORDER BY created_at DESC LIMIT 1"
    )


def is_admin(user) -> bool:
    """`auth.dependencies` fills `role` from `UserRole.value` -- "ADMIN",
    upper case. Comparing against "admin" silently never matched, so the
    bypass below never fired. Normalise rather than trust one spelling.
    """
    return str(getattr(user, "role", "") or "").upper() == UserRole.ADMIN.value


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
    if not is_admin(current_user) and owner_id != str(current_user.email):
        logger.warning(
            "[EVAL] Denied: user %s tried to evaluate session %s of agent %s (owner=%s)",
            current_user.email, session_id, agent_id, owner_id,
        )
        raise HTTPException(status_code=403, detail="Not your agent")

    # The model that actually produced THIS session, not the agent's
    # current configuration -- agents change model over time (see module
    # docstring / audit point 1), so comparing the judge against
    # `agent_model` could let a judge silently score its own production.
    usage_row = (
        await db.execute(_judged_model_sql(), {"session_id": session_id})
    ).first()
    if usage_row is not None and usage_row[0]:
        judged_model = usage_row[0]
        judged_model_source = "llm_usage"
    else:
        judged_model = agent.get("agent_model")
        judged_model_source = "agent_config_fallback"
        logger.info(
            "[EVAL] No llm_usage row for session %s; judged_model falls "
            "back to the agent's current config (%s), which may not be "
            "what produced this conversation",
            session_id, judged_model,
        )

    return SessionContext(
        agent_id=agent_id,
        app_name=app_name,
        session_user_id=session_user_id,
        judged_model=judged_model,
        judged_model_source=judged_model_source,
        owner_id=owner_id,
        agent_name=agent.get("agent_name") or app_name,
    )


async def owned_agent_ids(db: AsyncSession, current_user: user_schemas.User) -> set[int] | None:
    """None means unrestricted (admin). Otherwise the exact set of
    agent_ids this user owns -- callers must apply it when building the
    query, never after fetching rows.
    """
    if is_admin(current_user):
        return None

    from apowerb.core.agent_main import agent_store

    select_query = (
        agent_store.agent_table.select()
        .where(agent_store.agent_table.c.owner_id == current_user.email)
        .with_only_columns(agent_store.agent_table.c.agent_id)
    )
    rows = await asyncio.to_thread(agent_store.get_list_agents, select_query)
    return {row[0] for row in rows}


async def list_owned_agents(
    current_user: user_schemas.User,
    *,
    admin_sees_all: bool,
) -> list[tuple[int, str, str | None]]:
    """Every agent this user owns as `(agent_id, agent_name, owner_id)`.

    `admin_sees_all` says whether an administrator gets the whole platform
    or only their own agents, and it has no default on purpose. The two
    callers want opposite things: Supervision is an admin screen and
    crossing accounts is its job, while Evaluations is a product screen
    where it meant listing every agent on the platform next to its owner's
    email address. A default would let the next route inherit whichever
    answer happened to be written here.

    Carries the display name along so `GET /evaluations/agents` never has
    to look an agent's name up one at a time (the exact N+1 shape already
    paid for once on the Artefacts screen). One query on `agent_store`'s
    own synchronous connection, independent of the number of agents
    returned.
    """
    from apowerb.core.agent_main import agent_store

    select_query = agent_store.agent_table.select().with_only_columns(
        agent_store.agent_table.c.agent_id,
        agent_store.agent_table.c.agent_name,
        # An admin gets every agent, so the caller has to be able to say
        # whose each one is -- without this the screen can only claim they
        # are all yours, which is what it did.
        agent_store.agent_table.c.owner_id,
    )
    if not (admin_sees_all and is_admin(current_user)):
        select_query = select_query.where(
            agent_store.agent_table.c.owner_id == current_user.email
        )
    rows = await asyncio.to_thread(agent_store.get_list_agents, select_query)
    return [(row[0], row[1], row[2]) for row in rows]


# In-process, best-effort rate limit on POST /evaluations/run: the first
# reflex in front of a screen that looks broken is to click again, and
# unlike commercial quota (registered guards, see run_gate.py) this must
# hold even with no extension installed at all -- the OSS core's own
# defense against a re-click storm. Single-process state is enough for
# this: it survives exactly as long as the request rate it protects
# against (seconds), and a restart resetting it costs nothing.
_last_run_at: dict[tuple[str, str], float] = {}


def check_rerun_rate_limit(*, owner_id: str, session_id: str) -> None:
    """Raise 429 if this (owner, session) pair ran an evaluation less than
    `evaluation_min_rerun_interval_seconds` ago. Records the attempt
    whether it passes or is rejected, so a rapid burst of re-clicks stays
    rejected instead of each one resetting the window.
    """
    key = (owner_id, session_id)
    now = time.monotonic()
    last = _last_run_at.get(key)
    window = get_settings().evaluation_min_rerun_interval_seconds
    if last is not None and (now - last) < window:
        retry_after = round(window - (now - last), 1)
        raise HTTPException(
            status_code=429,
            detail=(
                "An evaluation for this session was started less than "
                f"{window}s ago; retry in {retry_after}s."
            ),
        )
    _last_run_at[key] = now


def _billing_extras(exc: Exception) -> dict:
    """A judge failure raised AFTER a real litellm call carries what that
    call cost (see `_shared_judge.attach_billing` /
    `task_completion_judge._attach_billing`). Lift it into the kwargs
    `EvaluationOutcome.not_applicable` merges into `details`, so
    `_record_judge_usage` bills it exactly like a success. Failures BEFORE
    any call (bad config, self-judge refusal, a connection error before a
    response exists) carry no such attribute -- nothing was spent.
    """
    usage = getattr(exc, "judge_usage", None)
    if usage is None:
        return {}
    return {
        "judge_usage": usage,
        "judge_model": getattr(exc, "judge_model", None),
        "judge_is_byom": getattr(exc, "judge_is_byom", False),
    }


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
    transcript: list[dict] | None = None,
) -> EvaluationOutcome:
    """Never raises: not configured, same-judge-as-judged, or any other
    failure (litellm timeout, malformed response, ...) all become a
    non-applicable outcome carrying the reason, so one judge's problem can
    never take another evaluator's already-computed result down with it.

    That guarantee is also what makes `asyncio.gather` safe over these:
    a coroutine that never raises cannot cancel its siblings.
    """
    try:
        outcome = await judge_fn(
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
            transcript=transcript,
        )
    except (SameJudgeError, RuntimeError) as exc:
        return EvaluationOutcome.not_applicable(
            evaluator=evaluator_name,
            kind="llm_judge",
            reason=str(exc),
            session_id=session_id,
            **_billing_extras(exc),
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
            **_billing_extras(exc),
        )
    else:
        if "judged_model" in outcome.details:
            outcome.details["judged_model_source"] = ctx.judged_model_source
        return outcome


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

    outcomes_by_name: dict[str, EvaluationOutcome] = {}

    # The deterministic evaluators run one after another, and before the
    # judges. They query the shared AsyncSession -- and roll it back when
    # the OTel tables are absent. A rollback is not a private act: a
    # concurrent coroutine would have its own work discarded by it. An
    # AsyncSession forbids concurrent operations anyway.
    if "tool_execution_outcome" in names:
        outcomes_by_name["tool_execution_outcome"] = await evaluate_tool_execution_outcome(
            db, session_id
        )
    if "tool_usage" in names:
        outcomes_by_name["tool_usage"] = await evaluate_tool_usage(db, session_id)

    # Resolved here, not at module level: the tests patch
    # `run_service.evaluate_*`, and a table built at import time would
    # capture the real functions and quietly ignore every patch.
    judge_fns = {
        "task_completion_judge": evaluate_task_completion,
        "coherence": evaluate_coherence,
        "completeness": evaluate_completeness,
        "hallucination": evaluate_hallucination,
    }
    judge_specs = [
        spec
        for spec in EVALUATOR_REGISTRY
        if spec.kind == "llm_judge" and spec.name in names
    ]

    if judge_specs:
        # The judges ARE the wall time: one LLM call each, awaited one
        # after another until now -- six evaluators took ~30s, which sat
        # right on the proxy's own limit and failed runs that had in fact
        # succeeded. They all read the same transcript, so it is fetched
        # once here: that removes three redundant queries AND leaves the
        # judges with no database work, which is what makes running them
        # concurrently safe on a session that permits none.
        transcript = await fetch_transcript(
            db,
            app_name=ctx.app_name,
            user_id=ctx.session_user_id,
            session_id=session_id,
        )
        gathered = await asyncio.gather(
            *(
                _run_llm_judge(
                    spec.name, judge_fns[spec.name], db, ctx, session_id,
                    judge_model=judge_model, judge_api_key=judge_api_key,
                    locale=locale, transcript=transcript,
                )
                for spec in judge_specs
            )
        )
        outcomes_by_name.update(
            zip((spec.name for spec in judge_specs), gathered, strict=True)
        )

    # Registry order, which is the order the callers were written to expect
    # and the one the screens sort by -- not the order the judges happened
    # to finish in.
    outcomes: list[EvaluationOutcome] = [
        outcomes_by_name[spec.name]
        for spec in EVALUATOR_REGISTRY
        if spec.name in outcomes_by_name
    ]

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

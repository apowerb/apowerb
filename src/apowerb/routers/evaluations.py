"""Evaluation results router.

Exposes the two evaluators already proven in `evaluation/evaluators/` over
HTTP: run one on a session, list stored results, and get an aggregate
summary. All three respond 404 when `evaluation_enabled` is false -- a
disabled feature does not announce its own existence, so the gate runs
before authentication -- and enforce agent ownership at the entry of the
query, never by filtering an already-built list (see
`evaluation/run_service.py` for why that ordering matters: it is the exact
BOLA shape already found once on this product's Logging page).

Every field on `EvaluationResultOut.details` is exactly what the evaluator
produced -- no renaming, no reshaping. `applicable` is the one field this
router adds on top of the stored row, computed the same way
`EvaluationOutcome.applicable` computes it: `score is not None`.

Judge model selection (`POST /run`): `judge_model` / `judge_api_key` let a
caller bring their own judge for a single run instead of the server's
shared one. `judge_model` without `judge_api_key` is rejected with 400
before anything else runs (before ownership is even resolved) -- a
server-side key must never be one missing field away from running
someone else's model. The key itself never appears in a log line, a
stored row, or a response: it is forwarded to `run_and_persist` and
nothing on this module's surface echoes it back.

`GET /evaluators` (contract-mandated) lists what's available so the front
builds its checkboxes off the API, never off a hardcoded list --
`run_service.EVALUATOR_REGISTRY` is the single source of truth this reads.
It also carries `judge_configured` (server-level, not per-item): whether
the server's shared judge is usable at all, so the front can grey out
`requires_judge` evaluators unless the caller also brings their own
model/key. Never the model name, never the key -- a plain boolean.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apowerb.auth.dependencies import get_current_user
from apowerb.configs.settings import get_settings
from apowerb.core.run_gate import apply_run_guards, resolve_owner_plan
from apowerb.evaluation.models import EvaluationResult
from apowerb.evaluation.run_service import (
    KNOWN_EVALUATORS,
    check_rerun_rate_limit,
    list_evaluator_specs,
    list_owned_agents,
    owned_agent_ids,
    resolve_session_context,
    run_and_persist,
)
from apowerb.helpers.database import get_db
from apowerb.users import schemas as user_schemas


async def _require_evaluation_enabled() -> None:
    if not get_settings().evaluation_enabled:
        raise HTTPException(status_code=404)


router = APIRouter(
    prefix="/evaluations",
    tags=["evaluations"],
    dependencies=[Depends(_require_evaluation_enabled)],
)


class EvaluationRunRequest(BaseModel):
    session_id: str
    evaluators: list[str] | None = None
    # Bring-your-own-model judge for this run only. Both absent: the
    # server's configured judge, as before. `judge_model` without
    # `judge_api_key` is rejected in `run_evaluation` -- never defaulted
    # to the server's key.
    judge_model: str | None = None
    judge_api_key: str | None = None
    # Language of `rationale` in every LLM-judge result -- it addresses the
    # person reading the screen, not the judged conversation, so it follows
    # the interface's own locale (the front sends next-intl's current one).
    # Never affects evaluator/criteria names: those are identifiers.
    locale: str = "en"


class EvaluationResultOut(BaseModel):
    id: int
    created_at: datetime
    agent_id: int
    run_id: uuid.UUID
    session_id: str
    evaluator_name: str
    evaluator_kind: str
    judge_model: str | None
    score: float | None
    passed: bool | None
    applicable: bool
    details: dict

    @classmethod
    def from_row(cls, row: EvaluationResult) -> "EvaluationResultOut":
        return cls(
            id=row.id,
            created_at=row.created_at,
            agent_id=row.agent_id,
            run_id=row.run_id,
            session_id=row.session_id,
            evaluator_name=row.evaluator_name,
            evaluator_kind=row.evaluator_kind,
            judge_model=row.judge_model,
            score=row.score,
            passed=row.passed,
            applicable=row.score is not None,
            details=row.details or {},
        )


class EvaluationRunResponse(BaseModel):
    session_id: str
    run_id: uuid.UUID
    results: list[EvaluationResultOut]


class EvaluationListResponse(BaseModel):
    items: list[EvaluationResultOut]
    total: int


class EvaluatorSummary(BaseModel):
    evaluator_name: str
    evaluator_kind: str
    runs: int
    applicable_runs: int
    passed: int
    pass_rate: float | None
    avg_score: float | None


class EvaluationSummaryResponse(BaseModel):
    since: datetime
    by_evaluator: list[EvaluatorSummary]


class EvaluatorInfo(BaseModel):
    name: str
    kind: str
    requires_judge: bool


class EvaluatorsListResponse(BaseModel):
    # True when the server's shared judge (EVALUATION_JUDGE_MODEL and
    # _API_KEY) is configured -- neither the model name nor the key
    # itself, a plain boolean the front uses to grey out `requires_judge`
    # evaluators unless the caller also supplies their own BYOM model/key.
    judge_configured: bool
    items: list[EvaluatorInfo]


@router.post("/run")
async def run_evaluation(
    request: EvaluationRunRequest,
    db: AsyncSession = Depends(get_db),
    current_user: user_schemas.User = Depends(get_current_user),
) -> EvaluationRunResponse:
    if request.judge_model and not request.judge_api_key:
        raise HTTPException(
            status_code=400,
            detail="judge_api_key is required when judge_model is provided",
        )
    ctx = await resolve_session_context(db, request.session_id, current_user)
    # Cheap, local re-click guard first (see run_service.check_rerun_rate_limit),
    # then the same choke point every other entry door goes through
    # (core.run_gate.apply_run_guards) -- both at the entry of the request,
    # before any evaluator runs, never after. Without this, POST /run was
    # the only LLM-spending route with no quota and no rate limit at all.
    check_rerun_rate_limit(owner_id=ctx.owner_id, session_id=request.session_id)
    await apply_run_guards(
        agent_name=ctx.app_name,
        owner_id=ctx.owner_id,
        plan=await resolve_owner_plan(ctx.owner_id),
    )
    # Generated here, not inside run_and_persist: it belongs at the root of
    # this response even when the evaluator list is empty, so this call is
    # the one place that must own it.
    run_id = uuid.uuid4()
    rows = await run_and_persist(
        db, ctx, request.session_id, request.evaluators,
        judge_model=request.judge_model,
        judge_api_key=request.judge_api_key,
        run_id=run_id,
        locale=request.locale,
    )
    return EvaluationRunResponse(
        session_id=request.session_id,
        run_id=run_id,
        results=[EvaluationResultOut.from_row(row) for row in rows],
    )


@router.get("/evaluators")
async def list_evaluators(
    current_user: user_schemas.User = Depends(get_current_user),
) -> EvaluatorsListResponse:
    settings = get_settings()
    judge_configured = bool(settings.evaluation_judge_model) and bool(
        settings.evaluation_judge_api_key
    )
    return EvaluatorsListResponse(
        judge_configured=judge_configured,
        items=[
            EvaluatorInfo(name=spec.name, kind=spec.kind, requires_judge=spec.requires_judge)
            for spec in list_evaluator_specs()
        ],
    )


@router.get("")
async def list_evaluations(
    agent_id: int | None = Query(default=None),
    session_id: str | None = Query(default=None),
    evaluator: str | None = Query(default=None),
    run_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: user_schemas.User = Depends(get_current_user),
) -> EvaluationListResponse:
    # Parsed before ownership is even resolved -- a malformed filter is a
    # request-shape problem, not something worth a query round-trip to reject.
    parsed_run_id: uuid.UUID | None = None
    if run_id is not None:
        try:
            parsed_run_id = uuid.UUID(run_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="run_id is not a valid UUID")

    owned = await owned_agent_ids(db, current_user)
    if owned is not None and agent_id is not None and agent_id not in owned:
        raise HTTPException(status_code=403, detail="Not your agent")
    if owned is not None and not owned:
        return EvaluationListResponse(items=[], total=0)

    filters = []
    if owned is not None:
        filters.append(EvaluationResult.agent_id.in_(owned))
    if agent_id is not None:
        filters.append(EvaluationResult.agent_id == agent_id)
    if session_id is not None:
        filters.append(EvaluationResult.session_id == session_id)
    if evaluator is not None:
        filters.append(EvaluationResult.evaluator_name == evaluator)
    if parsed_run_id is not None:
        filters.append(EvaluationResult.run_id == parsed_run_id)

    total = (
        await db.execute(
            select(func.count()).select_from(EvaluationResult).where(*filters)
        )
    ).scalar_one()

    result = await db.execute(
        select(EvaluationResult)
        .where(*filters)
        .order_by(EvaluationResult.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = result.scalars().all()

    return EvaluationListResponse(
        items=[EvaluationResultOut.from_row(row) for row in rows],
        total=total,
    )


@router.get("/summary")
async def evaluations_summary(
    agent_id: int | None = Query(default=None),
    days: int = Query(default=30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    current_user: user_schemas.User = Depends(get_current_user),
) -> EvaluationSummaryResponse:
    since = datetime.now(timezone.utc) - timedelta(days=days)

    owned = await owned_agent_ids(db, current_user)
    if owned is not None and agent_id is not None and agent_id not in owned:
        raise HTTPException(status_code=403, detail="Not your agent")
    if owned is not None and not owned:
        return EvaluationSummaryResponse(since=since, by_evaluator=[])

    filters = [EvaluationResult.created_at >= since]
    if owned is not None:
        filters.append(EvaluationResult.agent_id.in_(owned))
    if agent_id is not None:
        filters.append(EvaluationResult.agent_id == agent_id)

    applicable = EvaluationResult.score.isnot(None)
    passed_true = EvaluationResult.passed.is_(True)

    stmt = (
        select(
            EvaluationResult.evaluator_name,
            EvaluationResult.evaluator_kind,
            func.count().label("runs"),
            func.count().filter(applicable).label("applicable_runs"),
            func.count().filter(passed_true).label("passed"),
            func.avg(EvaluationResult.score).filter(applicable).label("avg_score"),
        )
        .where(*filters)
        .group_by(EvaluationResult.evaluator_name, EvaluationResult.evaluator_kind)
    )

    result = await db.execute(stmt)
    rows = result.all()

    by_evaluator = []
    for row in rows:
        applicable_runs = row.applicable_runs
        pass_rate = round(row.passed / applicable_runs, 4) if applicable_runs else None
        avg_score = round(float(row.avg_score), 4) if row.avg_score is not None else None
        by_evaluator.append(
            EvaluatorSummary(
                evaluator_name=row.evaluator_name,
                evaluator_kind=row.evaluator_kind,
                runs=row.runs,
                applicable_runs=applicable_runs,
                passed=row.passed,
                pass_rate=pass_rate,
                avg_score=avg_score,
            )
        )

    return EvaluationSummaryResponse(since=since, by_evaluator=by_evaluator)


# ---------------------------------------------------------------------------
# GET /agents, GET /runs -- state first, history second (contract v4)
#
# `GET /evaluations` above answers "what did I run"; these two answer "where
# do my agents stand" and "how did one agent's score move over time". Both
# read `EvaluationResult.details["not_applicable"]` back out onto the wire
# as `not_applicable` -- the same value `EvaluationOutcome.not_applicable`
# put there, never collapsed into a score of 0 (see evaluators/base.py).
#
# `_EVALUATOR_ORDER` fixes results within a run to `EVALUATOR_REGISTRY`'s
# declaration order, not `created_at` (all six rows of a run share a
# created_at to the microsecond, an unstable sort key) -- the screen aligns
# on six fixed columns, so the order must be deterministic and the same
# for every run.
# ---------------------------------------------------------------------------

_EVALUATOR_ORDER = {name: index for index, name in enumerate(KNOWN_EVALUATORS)}


def _sort_by_evaluator_order(rows: list[EvaluationResult]) -> list[EvaluationResult]:
    return sorted(rows, key=lambda row: _EVALUATOR_ORDER.get(row.evaluator_name, len(_EVALUATOR_ORDER)))


class EvaluationResultBrief(BaseModel):
    evaluator_name: str
    evaluator_kind: str
    score: float | None
    passed: bool | None
    # The reason, when score/passed are null -- never a zero standing in
    # for "nothing to judge". Null when the result IS applicable.
    not_applicable: str | None

    @classmethod
    def from_row(cls, row: EvaluationResult) -> "EvaluationResultBrief":
        return cls(
            evaluator_name=row.evaluator_name,
            evaluator_kind=row.evaluator_kind,
            score=row.score,
            passed=row.passed,
            not_applicable=(row.details or {}).get("not_applicable") if row.score is None else None,
        )


class AgentLastRunOut(BaseModel):
    run_id: uuid.UUID
    created_at: datetime
    session_id: str
    results: list[EvaluationResultBrief]


class AgentEvaluationStateOut(BaseModel):
    agent_id: int
    agent_name: str
    runs_count: int
    # None when this agent has never been evaluated -- a row, not an
    # absence: the front must be able to tell "no signal" from "loading".
    last_run: AgentLastRunOut | None


class AgentsEvaluationStateResponse(BaseModel):
    items: list[AgentEvaluationStateOut]


def _agent_state_sort_key(item: AgentEvaluationStateOut) -> tuple:
    # Never-evaluated agents first (they call for action) -- among those,
    # alphabetical; among evaluated agents, most recently evaluated first.
    if item.last_run is None:
        return (0, item.agent_name)
    return (1, -item.last_run.created_at.timestamp())


@router.get("/agents")
async def list_agents_evaluation_state(
    db: AsyncSession = Depends(get_db),
    current_user: user_schemas.User = Depends(get_current_user),
) -> AgentsEvaluationStateResponse:
    # Ownership resolved at the entry, through the same synchronous
    # `agent_store` connection every other route in this file uses --
    # this is also where the "never evaluated" agents come from: they
    # exist here and nowhere in `agent_evaluation_results`.
    agents = await list_owned_agents(current_user)
    if not agents:
        return AgentsEvaluationStateResponse(items=[])

    agent_ids = [agent_id for agent_id, _ in agents]

    # Query 1/3: how many distinct runs each agent has -- one grouped
    # query for every owned agent, not one per agent.
    counts_stmt = (
        select(
            EvaluationResult.agent_id,
            func.count(func.distinct(EvaluationResult.run_id)).label("runs_count"),
        )
        .where(EvaluationResult.agent_id.in_(agent_ids))
        .group_by(EvaluationResult.agent_id)
    )
    counts_rows = (await db.execute(counts_stmt)).all()
    runs_count_by_agent = {row.agent_id: row.runs_count for row in counts_rows}

    # Query 2/3: this agent's most recent run_id, via a window function
    # (portable across Postgres and SQLite, unlike DISTINCT ON) instead of
    # one "last run" query per agent -- the N+1 shape already paid for
    # once on the Artefacts screen (module docstring).
    row_number = func.row_number().over(
        partition_by=EvaluationResult.agent_id,
        order_by=EvaluationResult.created_at.desc(),
    ).label("rn")
    ranked = (
        select(EvaluationResult.agent_id, EvaluationResult.run_id, row_number)
        .where(EvaluationResult.agent_id.in_(agent_ids))
        .subquery()
    )
    last_run_stmt = select(ranked.c.agent_id, ranked.c.run_id).where(ranked.c.rn == 1)
    last_run_rows = (await db.execute(last_run_stmt)).all()
    last_run_id_by_agent = {row.agent_id: row.run_id for row in last_run_rows}

    # Query 3/3: every result row of every agent's last run, in one shot --
    # a run has all six evaluators or fewer, never partially fetched.
    results_by_run: dict[uuid.UUID, list[EvaluationResult]] = {}
    if last_run_id_by_agent:
        results_stmt = select(EvaluationResult).where(
            EvaluationResult.run_id.in_(last_run_id_by_agent.values())
        )
        for row in (await db.execute(results_stmt)).scalars().all():
            results_by_run.setdefault(row.run_id, []).append(row)

    items = []
    for agent_id, agent_name in agents:
        run_id = last_run_id_by_agent.get(agent_id)
        last_run = None
        if run_id is not None:
            run_rows = _sort_by_evaluator_order(results_by_run.get(run_id, []))
            first = run_rows[0]
            last_run = AgentLastRunOut(
                run_id=run_id,
                created_at=first.created_at,
                session_id=first.session_id,
                results=[EvaluationResultBrief.from_row(row) for row in run_rows],
            )
        items.append(
            AgentEvaluationStateOut(
                agent_id=agent_id,
                agent_name=agent_name,
                runs_count=runs_count_by_agent.get(agent_id, 0),
                last_run=last_run,
            )
        )

    items.sort(key=_agent_state_sort_key)
    return AgentsEvaluationStateResponse(items=items)


class EvaluationRunOut(BaseModel):
    run_id: uuid.UUID
    created_at: datetime
    session_id: str
    results: list[EvaluationResultBrief]


class EvaluationRunsResponse(BaseModel):
    items: list[EvaluationRunOut]
    total: int


@router.get("/runs")
async def list_evaluation_runs(
    agent_id: int = Query(...),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: user_schemas.User = Depends(get_current_user),
) -> EvaluationRunsResponse:
    owned = await owned_agent_ids(db, current_user)
    if owned is not None and agent_id not in owned:
        raise HTTPException(status_code=403, detail="Not your agent")

    total = (
        await db.execute(
            select(func.count(func.distinct(EvaluationResult.run_id))).where(
                EvaluationResult.agent_id == agent_id
            )
        )
    ).scalar_one()

    run_page_stmt = (
        select(
            EvaluationResult.run_id,
            func.max(EvaluationResult.created_at).label("created_at"),
        )
        .where(EvaluationResult.agent_id == agent_id)
        .group_by(EvaluationResult.run_id)
        .order_by(func.max(EvaluationResult.created_at).desc())
        .limit(limit)
        .offset(offset)
    )
    run_page_rows = (await db.execute(run_page_stmt)).all()

    results_by_run: dict[uuid.UUID, list[EvaluationResult]] = {}
    if run_page_rows:
        run_ids = [row.run_id for row in run_page_rows]
        results_stmt = select(EvaluationResult).where(EvaluationResult.run_id.in_(run_ids))
        for row in (await db.execute(results_stmt)).scalars().all():
            results_by_run.setdefault(row.run_id, []).append(row)

    items = []
    for run_row in run_page_rows:
        run_rows = _sort_by_evaluator_order(results_by_run.get(run_row.run_id, []))
        first = run_rows[0]
        items.append(
            EvaluationRunOut(
                run_id=run_row.run_id,
                created_at=run_row.created_at,
                session_id=first.session_id,
                results=[EvaluationResultBrief.from_row(row) for row in run_rows],
            )
        )

    return EvaluationRunsResponse(items=items, total=total)

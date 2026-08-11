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
from apowerb.evaluation.models import EvaluationResult
from apowerb.evaluation.run_service import (
    list_evaluator_specs,
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

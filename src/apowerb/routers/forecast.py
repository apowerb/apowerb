"""Router for POST /api/v1/forecast — authenticated relay to th2forecast.

Mount in main.py:
    from apowerb.routers.forecast import router as forecast_router
    api_router.include_router(forecast_router, prefix="/api/v1")

Boucle fermée (contrat étape 5 §3) : un `chart_id` optionnel relie l'appel à
un graphique BI existant. S'il est fourni, il doit être lisible par
l'appelant (même contrôle d'accès propriétaire/organisation que
`GET /api/v1/charts/{id}` — ``DatabaseChartStore`` scopé par `owner`), sinon
la requête répond 404 avant tout appel au moteur. `chart_id` n'est jamais
relayé à th2forecast. Quand l'accès est validé, la réponse gagne un champ
`tracking` (couverture des bandes, MASE, ruptures — voir
``apowerb.bi.forecast_tracking``) comparant l'historique RÉGULARISÉ renvoyé
par le moteur (`series[].history`, jamais le `data` brut de la requête) aux
instantanés précédents de même configuration, et une version élaguée de
cette réponse (group/level/model/forecast seulement) est stockée comme
nouvel instantané (table ``bi_forecast_snapshots``, 60 conservés par
graphique). Un échec de stockage est journalisé et n'empêche pas la
prévision : la réponse est alors rendue sans `tracking`.

``client.forecast()`` est un appel bloquant (``requests`` + attente de
polling) : il est déporté via ``asyncio.to_thread`` pour ne pas geler la
boucle asyncio pendant tout le calcul.
"""
from __future__ import annotations

import asyncio
from datetime import date as date_type
from logging import getLogger
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from apowerb.auth.dependencies import get_current_user
from apowerb.bi.charts.service import ChartNotFoundError, ChartService
from apowerb.bi.db_stores import DatabaseChartStore
from apowerb.bi.forecast_snapshot_store import ForecastSnapshotStore
from apowerb.bi.forecast_tracking import compute_config_hash, compute_tracking, prunable_snapshot_payload
from apowerb.helpers.database import get_db
from apowerb.integrations.th2forecast_client import (
    Th2forecastAPIError,
    Th2forecastClient,
    Th2forecastNotConfigured,
    Th2forecastTimeout,
    Th2forecastUnavailable,
)
from apowerb.schema.forecast_schema import ForecastInterpretSchema, ForecastRequestSchema
from apowerb.users import schemas as user_schemas

logger = getLogger(__name__)

router = APIRouter(tags=["forecast"])

CurrentUser = Annotated[user_schemas.User, Depends(get_current_user)]
DbSession = Annotated[AsyncSession, Depends(get_db)]

# Champs relayés au moteur uniquement s'ils sont fournis — même piège que
# events/scenarios : Pydantic jette un champ inconnu en silence, donc un
# champ absent doit rester absent plutôt que passer `null`.
_OMIT_WHEN_NONE = ("events", "scenarios", "hierarchy", "reconciliation")
# Jamais relayé : le cœur seul en a besoin (boucle fermée, contrat étape 5 §3).
_NEVER_RELAYED = ("chart_id",)


def _error_body(message: str, *, field: str | None = None) -> dict:
    return {"status": "error", "errors": [{"field": field, "message": message}]}


def _relay_payload(body: ForecastRequestSchema) -> dict:
    payload = body.model_dump(exclude_none=False)
    for key in _NEVER_RELAYED:
        payload.pop(key, None)
    for key in _OMIT_WHEN_NONE:
        if payload.get(key) is None:
            payload.pop(key, None)
    return payload


def _history_end(series: list[dict]) -> date_type | None:
    """La dernière date de l'historique RÉGULARISÉ renvoyé par le moteur
    (`series[].history`, toujours ISO `YYYY-MM-DD`) — sert de clé de version
    à l'instantané et de repère pour le suivi (contrat étape 5 §3). Le `data`
    brut de la requête n'est jamais utilisé ici : son format de date peut
    différer (`2024/01/05`, avec heure...) et ses groupes peuvent être
    numériques, alors que le moteur régularise les deux."""
    dates = [
        point.get("date")
        for s in series
        for point in (s.get("history") or [])
        if point.get("date")
    ]
    if not dates:
        return None
    latest = max(dates)
    return latest if isinstance(latest, date_type) else date_type.fromisoformat(str(latest))


@router.post("/forecast")
async def create_forecast(body: ForecastRequestSchema, current_user: CurrentUser, db: DbSession):
    """Run a forecast via th2forecast and relay its response as-is.

    th2forecast's own errors (400/401/413) are relayed with their original
    status code and JSON body. A missing TH2FORECAST_URL answers 503 with the
    message the contract fixes. A network failure or a global-timeout expiry
    answers 502/504 with the same error shape.

    With `chart_id` (boucle fermée, contrat étape 5 §3): the chart must exist
    and belong to the caller (same access control as reading it) or the
    request answers 404 before the engine is ever called. On success, the
    response gains a `tracking` field comparing this request's history to the
    chart's earlier forecast snapshots, and this response is itself stored as
    the newest snapshot. A storage/tracking failure is logged and does not
    fail the forecast — the plain relayed body is returned without `tracking`.
    """
    chart = None
    if body.chart_id:
        chart_store = DatabaseChartStore(db, owner=current_user.email)
        try:
            chart = await ChartService(chart_store, db).get(body.chart_id)
        except ChartNotFoundError:
            # Ne révèle pas si le graphique existe pour quelqu'un d'autre.
            return JSONResponse(status_code=404, content=_error_body("Graphique introuvable."))

    try:
        client = Th2forecastClient()
    except Th2forecastNotConfigured:
        return JSONResponse(
            status_code=503,
            content=_error_body("Service de prévision non configuré (TH2FORECAST_URL)"),
        )

    try:
        payload = _relay_payload(body)
        # client.forecast() est bloquant (requests + sleep de polling) : hors
        # thread, il gèlerait toute la boucle asyncio pendant tout le calcul.
        result = await asyncio.to_thread(client.forecast, payload)
    except Th2forecastAPIError as exc:
        return JSONResponse(status_code=exc.status_code, content=exc.body)
    except Th2forecastTimeout as exc:
        logger.error("forecast request for user %s timed out: %s", current_user.user_id, exc)
        return JSONResponse(
            status_code=504,
            content=_error_body("Le service de prévision n'a pas répondu dans le délai imparti."),
        )
    except Th2forecastUnavailable as exc:
        logger.error("forecast request for user %s failed: %s", current_user.user_id, exc)
        return JSONResponse(
            status_code=502,
            content=_error_body("Service de prévision indisponible."),
        )

    if chart is not None:
        try:
            result = await _track_and_store(
                db, chart=chart, owner=current_user.email, payload=payload, result=result
            )
        except Exception:
            # Un échec de stockage/suivi ne casse pas la prévision : on la
            # rend telle quelle, sans `tracking`.
            logger.error(
                "forecast tracking/storage failed for chart %s (user %s)",
                body.chart_id, current_user.user_id, exc_info=True,
            )

    return result


async def _track_and_store(
    db: AsyncSession, *, chart, owner: str, payload: dict, result: dict
) -> dict:
    """Suivi puis instantané (contrat étape 5 §3 : dans cet ordre). Les
    actuels du suivi et `history_end` viennent de `result["series"]`
    (réponse du moteur, régularisée) — jamais de `data` brut."""
    series = result.get("series", [])
    history_end = _history_end(series)
    if history_end is None:
        logger.warning("forecast response for chart %s has no series history to track", chart.id)
        return result

    config_hash = compute_config_hash(payload)
    snapshot_store = ForecastSnapshotStore(db)
    existing = await snapshot_store.list_for_chart(chart.id)
    snapshots = [
        {"config_hash": s.config_hash, "history_end": s.history_end.isoformat(), "payload": s.payload}
        for s in existing
    ]
    tracking = compute_tracking(series=series, snapshots=snapshots, config_hash=config_hash)

    await snapshot_store.upsert(
        chart_id=chart.id,
        owner=owner,
        organization_id=chart.organization_id,
        config_hash=config_hash,
        history_end=history_end,
        frequency=payload.get("frequency"),
        payload=prunable_snapshot_payload(result),
    )

    return {**result, "tracking": tracking}


@router.post("/forecast/interpret")
async def interpret_forecast_context(body: ForecastInterpretSchema, current_user: CurrentUser):
    """Free-text business context -> events and scenarios, checked before return.

    404 when switched off, 402 when the shared model's cap is reached, 503 when
    the model does not answer usefully, 422 when the text holds no usable context.
    """
    from apowerb.core import forecast_context

    known = forecast_context.known_events(body.events, groups=body.groups)
    return await forecast_context.interpret(
        body.text,
        owner_id=current_user.email,
        history_start=body.history_start,
        history_end=body.history_end,
        horizon_end=body.horizon_end,
        frequency=body.frequency,
        groups=body.groups,
        known_events=known,
    )

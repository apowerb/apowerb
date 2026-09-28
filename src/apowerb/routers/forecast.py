"""Router for POST /api/v1/forecast — authenticated relay to th2forecast.

Mount in main.py:
    from apowerb.routers.forecast import router as forecast_router
    api_router.include_router(forecast_router, prefix="/api/v1")
"""
from __future__ import annotations

from logging import getLogger
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from apowerb.auth.dependencies import get_current_user
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


def _error_body(message: str, *, field: str | None = None) -> dict:
    return {"status": "error", "errors": [{"field": field, "message": message}]}


@router.post("/forecast")
def create_forecast(body: ForecastRequestSchema, current_user: CurrentUser):
    """Run a forecast via th2forecast and relay its response as-is.

    th2forecast's own errors (400/401/413) are relayed with their original
    status code and JSON body. A missing TH2FORECAST_URL answers 503 with the
    message the contract fixes. A network failure or a global-timeout expiry
    answers 502/504 with the same error shape.
    """
    try:
        client = Th2forecastClient()
    except Th2forecastNotConfigured:
        return JSONResponse(
            status_code=503,
            content=_error_body("Service de prévision non configuré (TH2FORECAST_URL)"),
        )

    try:
        payload = body.model_dump(exclude_none=False)
        # Absents plutôt que null : th2forecast n'ajoute alors rien à sa réponse.
        for key in ("events", "scenarios"):
            if payload[key] is None:
                del payload[key]
        result = client.forecast(payload)
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

    return result


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

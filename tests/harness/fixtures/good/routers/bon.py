"""Harness example: the exception is logged, the client gets a fixed message or the engine's safe fields."""
from logging import getLogger

from fastapi import HTTPException

from apowerb.core.workflow_engine import error_fields
from apowerb.helpers.error_responses import safe_error_message

logger = getLogger(__name__)


def safe(call):
    try:
        return call()
    except ValueError as exc:
        raise HTTPException(
            status_code=500,
            detail=safe_error_message(
                exc, logger=logger, context="example", client_message="The request failed."
            ),
        )
    except Exception as exc:
        return {"event": "error", **error_fields(exc)}

"""Router for Data Lake (pins/S3 storage) and Data Handler (filter/aggregate) endpoints."""

from logging import getLogger

from fastapi import APIRouter, Depends, HTTPException

from apowerb.auth.dependencies import get_current_user
from apowerb.helpers.data_lake import StorageBoardFactory
from apowerb.schema.data_lake_schema import (
    BoardConfigSchema,
    PinReadSchema,
    PinWriteSchema,
)

from apowerb.users import schemas as user_schemas

logger = getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_board(bucket_name: str, prefix: str, storage_source: str = "s3"):
    """Build a pins board from parameters."""
    factory = StorageBoardFactory(bucket_name=bucket_name, prefix=prefix)
    return factory.get_board(storage_source=storage_source)


# ---------------------------------------------------------------------------
# Data Lake -- Board / Pin endpoints
# ---------------------------------------------------------------------------


@router.post("/data-lake/pins/list", tags=["data-lake"])
async def list_pins(
    body: BoardConfigSchema,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """List all pins available in a storage board."""
    try:
        board = _build_board(body.bucket_name, body.prefix, body.storage_source)
        pin_list = board.pin_list()
        return {
            "success": True,
            "pins": pin_list.to_dict(orient="records")
            if hasattr(pin_list, "to_dict")
            else list(pin_list),
        }
    except Exception as exc:
        logger.error("Failed to list pins: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/data-lake/pins/read", tags=["data-lake"])
async def read_pin(
    body: PinReadSchema,
    pin_name: str,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Read a specific pin from the storage board.

    The pin_name is passed as a query parameter, board config in the body.
    """
    try:
        board = _build_board(body.bucket_name, body.prefix, body.storage_source)
        data = board.pin_read(pin_name)
        # pins returns a pandas DataFrame -- convert to list of dicts
        if hasattr(data, "to_dict"):
            records = data.to_dict(orient="records")
        else:
            records = list(data)
        return {
            "success": True,
            "pin_name": pin_name,
            "row_count": len(records),
            "data": records,
        }
    except Exception as exc:
        logger.error("Failed to read pin '%s': %s", pin_name, exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/data-lake/pins/write", tags=["data-lake"])
async def write_pin(
    body: PinWriteSchema,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Write data to a pin in the storage board."""
    try:
        board = _build_board(body.bucket_name, body.prefix, body.storage_source)
        board.pin_write(body.pin_name, body.data, type=body.pin_type)
        return {
            "success": True,
            "message": f"Pin '{body.pin_name}' written successfully (type={body.pin_type}).",
        }
    except Exception as exc:
        logger.error("Failed to write pin '%s': %s", body.pin_name, exc)
        raise HTTPException(status_code=500, detail=str(exc))

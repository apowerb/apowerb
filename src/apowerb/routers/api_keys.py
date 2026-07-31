from fastapi import APIRouter, Depends, HTTPException
from apowerb.core.api_key_main import (
    register_api_key,
    list_user_api_keys,
    delete_api_key,
)
from apowerb.schema.api_key_schema import ApiKeyCreateSchema
from apowerb.auth.dependencies import get_current_user
from apowerb.users import schemas as user_schemas
from apowerb.helpers.emails import get_domain_from_email

router = APIRouter()


@router.get("/saved-api-keys", tags=["api-keys"])
async def list_keys(
    current_user: user_schemas.User = Depends(get_current_user),
):
    """List saved API keys for the current user."""
    return list_user_api_keys(current_user.email)


@router.post("/saved-api-keys", tags=["api-keys"])
async def create_key(
    data: ApiKeyCreateSchema,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Save a new API key."""
    org = get_domain_from_email(current_user.email)
    data_with_owner = data.model_copy(
        update={"owner_id": current_user.email, "organization_id": org}
    )
    return register_api_key(data_with_owner)


@router.delete("/saved-api-keys/{api_key_id}", tags=["api-keys"])
async def remove_key(
    api_key_id: str,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Delete a saved API key."""
    try:
        result = delete_api_key(api_key_id, user_id=current_user.email)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid API key ID format")
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result

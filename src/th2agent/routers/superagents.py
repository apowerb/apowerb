from fastapi import APIRouter, Depends
from th2agent.core.superagents import list_superagent_templates, get_superagent_template
from th2agent.auth.dependencies import get_current_user
from th2agent.users import schemas as user_schemas

router = APIRouter()


@router.get("/superagents", tags=["superagents"])
async def list_templates(
    current_user: user_schemas.User = Depends(get_current_user),
):
    """List SuperAgent templates visible to the authenticated user.

    Templates marked ``visible_to_orgs`` are filtered out for callers
    outside those orgs (e.g. ``scei_ar_assistant`` is hidden from non-SCEI
    users).
    """
    return list_superagent_templates(user=current_user)


@router.get("/superagents/{template_id}", tags=["superagents"])
async def get_template(
    template_id: str,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Get a specific SuperAgent template by id.

    Returns the legacy ``{"message": ...}`` payload both when the
    template does not exist and when it exists but is restricted to an
    org the caller does not belong to — the two cases are intentionally
    indistinguishable so org membership is not leaked through the
    response shape.
    """
    template = get_superagent_template(template_id, user=current_user)
    if template:
        return template
    return {"message": "SuperAgent template not found."}

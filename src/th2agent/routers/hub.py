from fastapi import APIRouter, Depends, HTTPException
from th2agent.core.hub_main import (
    publish_agent,
    list_hub_agents,
    get_hub_agent,
    clone_hub_agent,
    delete_hub_agent,
)
from th2agent.schema.hub_schema import HubPublishSchema, HubCloneSchema
from th2agent.auth.dependencies import get_current_user
from th2agent.users import schemas as user_schemas
from th2agent.helpers.emails import get_domain_from_email

router = APIRouter()


@router.get("/hub", tags=["hub"])
async def list_hub(
    current_user: user_schemas.User = Depends(get_current_user),
):
    """List all agents published in the Hub."""
    return list_hub_agents()


@router.get("/hub/{hub_id}", tags=["hub"])
async def get_hub(
    hub_id: str,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Get a specific hub agent by ID."""
    agent = get_hub_agent(hub_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Hub agent not found.")
    return agent


@router.post("/hub/publish", tags=["hub"])
async def publish(
    data: HubPublishSchema,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Publish an agent to the Hub."""
    org = get_domain_from_email(current_user.email)
    return publish_agent(data, user_id=current_user.email, org_id=org)


@router.post("/hub/clone", tags=["hub"])
async def clone(
    data: HubCloneSchema,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Clone an agent from the Hub into your own agent store."""
    org = get_domain_from_email(current_user.email)
    return clone_hub_agent(data.hub_agent_id, user_id=current_user.email, org_id=org, clone_name=data.clone_name)


@router.delete("/hub/{hub_id}", tags=["hub"])
async def remove_from_hub(
    hub_id: str,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Remove an agent from the Hub (publisher only)."""
    return delete_hub_agent(hub_id, user_id=current_user.email)

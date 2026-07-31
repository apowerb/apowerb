from fastapi import APIRouter, Depends, HTTPException
from apowerb.core.agent_main import (
    delete_agent,
    register_agent,
    fetch_agents,
    get_agent,
    get_agent_template_status,
    resync_agent_to_template,
)
from apowerb.schema.agent_schema import AgentCreateSchema
from apowerb.auth.dependencies import get_current_user
from apowerb.users import schemas as user_schemas
from apowerb.helpers.emails import get_domain_from_email
from apowerb.core.agent_main import update_agent as update_agent_func
from apowerb.configs.th2logger import setup_logging
from apowerb.core.agent_helpers.llm_model_builder import validate_agent_model

router = APIRouter()
logger = setup_logging(__name__)


@router.get("/agents", tags=["agents"])
async def list_agents(current_user: user_schemas.User = Depends(get_current_user)):
    """Endpoint to list all agents."""
    agents = fetch_agents(user_id=current_user.email)
    return agents


@router.post("/agents", tags=["agents"])
async def create_agent(
    agent_data: AgentCreateSchema,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """
    Endpoint to create a new agent.
    - No Mage triggers are created during agent creation
    - Triggers are lazily created when first scheduling a run via /schedule_run
    - This allows for flexible scheduling (cron expressions, @hourly, @daily, etc.)
    """
    # Create a new schema instance with owner_id
    organization_d = get_domain_from_email(current_user.email)
    agent_data_with_owner = agent_data.model_copy(
        update={"owner_id": current_user.email, "organization_id": organization_d}
    )

    # Fail-fast : provider du modele invalide -> 422 (pas un 500 muet, ni un agent
    # cree puis muet a chaque tour). NO-OP pour les conteneurs sequential/loop.
    try:
        validate_agent_model(
            agent_data.agent_model, agent_data.agent_model_params, agent_data.agent_type
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    # Register the agent
    result = register_agent(agent_data_with_owner)

    logger.info(f" Agent created successfully: {result.get('agent_id')}")
    logger.info(" Schedule trigger will be created on first /schedule_run call")

    return result


@router.get("/agents/{agent_id}", tags=["agents"])
async def read_agent(
    agent_id: str, current_user: user_schemas.User = Depends(get_current_user)
):
    """Endpoint to get a specific agent by ID."""
    agent = get_agent(int(agent_id.replace("agent", "")), user_id=current_user.email)
    if agent:
        return agent
    return {"message": "Agent not found."}


@router.put("/agents/{agent_id}", tags=["agents"])
async def update_agent(
    agent_id: str,
    agent_data: AgentCreateSchema,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Endpoint to update an existing agent."""
    # Strip 'agent' prefix if present to match DB ID
    clean_id = int(agent_id.replace("agent", ""))
    organization_d = get_domain_from_email(current_user.email)
    agent_data_with_owner = agent_data.model_copy(
        update={"owner_id": current_user.email, "organization_id": organization_d}
    )
    try:
        validate_agent_model(
            agent_data.agent_model, agent_data.agent_model_params, agent_data.agent_type
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    result = update_agent_func(
        clean_id, agent_data_with_owner, user_id=current_user.email
    )
    return result


@router.delete("/agents/{agent_id}", tags=["agents"])
async def remove_agent(
    agent_id: str, current_user: user_schemas.User = Depends(get_current_user)
):
    """Endpoint to delete an agent by ID."""
    # Strip 'agent' prefix if present to match DB ID
    clean_id = agent_id.replace("agent", "")
    delete_agent(clean_id, user_id=current_user.email)
    return {"message": "Agent deleted successfully."}


@router.get("/agents/{agent_id}/template-status", tags=["agents"])
async def template_status(
    agent_id: str,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Compare the agent's stored template snapshot with the live template.

    Returned shape::

        {
            "agent_id": 6,
            "template_id": "scei_ar_assistant" | null,
            "is_in_sync": true | false,
            "stored_hash": "abc..." | null,
            "current_hash": "def..." | null,
            "drift_fields": ["agent_instruction", "agent_tools", ...]
        }

    The frontend uses this to surface a "template updated, click to sync"
    banner on the agent's edit page.
    """
    clean_id = int(agent_id.replace("agent", ""))
    return get_agent_template_status(clean_id, user_id=current_user.email)


@router.post("/agents/{agent_id}/resync-template", tags=["agents"])
async def resync_template(
    agent_id: str,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Overwrite the agent's hash-relevant fields with the live template.

    Touches only ``agent_instruction``, ``agent_tools`` and ``tags`` —
    user-owned knobs (model, model_params, mcp_servers, guardrails,
    artifacts/memory toggles, agent_skills) are left untouched.

    Returns the post-resync ``template-status`` report (which should now
    show ``is_in_sync: true``).
    """
    clean_id = int(agent_id.replace("agent", ""))
    return resync_agent_to_template(clean_id, user_id=current_user.email)

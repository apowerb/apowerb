from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from apowerb.core.agent_main import (
    delete_agent,
    register_agent,
    fetch_agents,
    get_agent,
    get_agent_template_status,
    resync_agent_to_template,
    list_agent_revisions,
    restore_agent_revision,
)
from apowerb.schema.agent_schema import AgentCreateSchema
from apowerb.auth.dependencies import get_current_user
from apowerb.users import schemas as user_schemas
from apowerb.helpers.emails import get_domain_from_email
from apowerb.core.agent_main import update_agent as update_agent_func
from apowerb.configs.th2logger import setup_logging
from apowerb.core.agent_helpers.llm_model_builder import validate_agent_model
from apowerb.routers.agent_reload import invalidate_agent_runtime

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
    raise HTTPException(status_code=404, detail="Agent not found.")


@router.put("/agents/{agent_id}", tags=["agents"])
async def update_agent(
    agent_id: str,
    agent_data: AgentCreateSchema,
    request: Request,
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
    invalidate_agent_runtime(request.app.state, str(clean_id))
    return result


@router.patch("/agents/{agent_id}", tags=["agents"])
async def patch_agent(
    agent_id: str,
    request: Request,
    changes: dict = Body(...),
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Update only the fields sent; every other field keeps its stored value.

    The stored agent is read as ``GET /api/agents/{id}`` returns it (API key
    masked, which the write swaps back), merged with ``changes``, validated
    like a PUT body and written through the PUT.
    """
    unknown = sorted(set(changes) - set(AgentCreateSchema.model_fields))
    if unknown:
        raise HTTPException(
            status_code=422, detail=f"Unknown agent fields: {', '.join(unknown)}"
        )
    clean_id = int(agent_id.replace("agent", ""))
    current = get_agent(clean_id, user_id=current_user.email)
    if not current:
        raise HTTPException(status_code=404, detail="Agent not found.")
    try:
        agent_data = AgentCreateSchema.model_validate({**current, **changes})
    except ValidationError as exc:
        raise RequestValidationError(exc.errors(include_url=False)) from exc
    return await update_agent(agent_id, agent_data, request, current_user)


@router.delete("/agents/{agent_id}", tags=["agents"])
async def remove_agent(
    agent_id: str,
    request: Request,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Endpoint to delete an agent by ID."""
    # Strip 'agent' prefix if present to match DB ID
    clean_id = agent_id.replace("agent", "")
    delete_agent(clean_id, user_id=current_user.email)
    invalidate_agent_runtime(request.app.state, clean_id)
    return {"message": "Agent deleted successfully."}


@router.get("/agents/{agent_id}/revisions", tags=["agents"])
async def agent_revisions(
    agent_id: str,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """List the definitions this agent overwrote, newest first.

    Chaque entrée porte ``revision_id``, ``revised_at``, ``revised_by``,
    ``reason`` ("update", "resync" ou "restore") et ``changed_fields`` — les
    champs qui diffèrent de la définition actuellement en service.

    La ligne archivée elle-même n'est pas renvoyée : elle contient la clé API
    chiffrée, qui n'a rien à faire dans une réponse HTTP.
    """
    clean_id = int(agent_id.replace("agent", ""))
    return list_agent_revisions(clean_id, user_id=current_user.email)


@router.post("/agents/{agent_id}/revisions/{revision_id}/restore", tags=["agents"])
async def restore_agent(
    agent_id: str,
    revision_id: int,
    request: Request,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Put an archived definition back in service, without a redeploy.

    L'état courant est archivé au passage, donc une restauration se défait
    comme n'importe quelle autre modification. Le module ADK est réécrit sur
    disque dans la foulée : l'agent répond avec la définition restaurée sans
    redémarrage du service.
    """
    clean_id = int(agent_id.replace("agent", ""))
    result = restore_agent_revision(clean_id, revision_id, user_id=current_user.email)
    invalidate_agent_runtime(request.app.state, str(clean_id))
    return result


@router.get("/agents/{agent_id}/template-status", tags=["agents"])
async def template_status(
    agent_id: str,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Compare the agent's stored template snapshot with the live template.

    Returned shape::

        {
            "agent_id": 6,
            "template_id": "a_template_id" | null,
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
    request: Request,
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
    result = resync_agent_to_template(clean_id, user_id=current_user.email)
    invalidate_agent_runtime(request.app.state, str(clean_id))
    return result

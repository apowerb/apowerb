from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from apowerb.auth.dependencies import get_current_user
from apowerb.users import schemas as user_schemas
from apowerb.scheduler.mage import get_orchestrator, process_agent_registration
from apowerb.scheduler.th2etl_client import OrchestratorUnavailable
from apowerb.core.agent_main import fetch_agents
from apowerb.configs.th2logger import setup_logging

logger = setup_logging(__name__)

router = APIRouter()


class _OrchestratorClientProxy:
    """Résout le client d'orchestration à l'appel, jamais à l'import.

    Honore le réglage ORCHESTRATOR ("mage" par défaut | "th2etl") au lieu de
    figer MageAPIClient : sinon l'UI Orchestrateur (pipelines/schedules/runs/
    cancel) continue de lire MageAI après une bascule vers th2etl, et les runs
    th2etl n'y apparaissent jamais.

    Pourquoi un proxy et pas une simple constante : ``get_orchestrator().client``
    au niveau module construisait un client HTTP au seul fait d'importer ce
    routeur — un consommateur de la library qui monte ``apowerb.routers`` sur
    sa propre app payait la construction, et figeait le choix d'orchestrateur au
    moment de l'import plutôt qu'à celui de la requête. Le proxy garde le même
    nom, donc les tests qui remplacent ``scheduler_client`` continuent de marcher.
    """

    def __getattr__(self, name: str):
        return getattr(get_orchestrator().client, name)


scheduler_client = _OrchestratorClientProxy()


def _get_user_agent_ids(user_id: str) -> set[str]:
    """Return the set of agent IDs owned by a user, in all known formats.

    Mage trigger names may be stored as "75" or "agent75" depending
    on how they were created, so we include both variants.
    """
    agents = fetch_agents(user_id)
    ids: set[str] = set()
    for a in agents:
        aid = a.get("agent_id")
        if aid is not None:
            ids.add(str(aid))  # "75"
            ids.add(f"agent{aid}")  # "agent75"
    return ids


class CreateTriggerRequest(BaseModel):
    agent_id: str
    agent_name: str
    agent_model: str | None = None
    agent_description: str | None = None


@router.get("/pipelines", tags=["scheduler"])
async def list_pipelines(current_user: user_schemas.User = Depends(get_current_user)):
    """Endpoint to list all available pipelines."""
    try:
        return scheduler_client.get_all_pipelines()
    except OrchestratorUnavailable as e:
        # 503, never 200 with an empty list: the dashboard must be able to say
        # "the orchestrator is down" instead of "you have no pipelines".
        logger.error("orchestrator unreachable while listing pipelines: %s", e)
        raise HTTPException(status_code=503, detail=str(e)) from e


@router.post("/pipelines/agents/triggers", tags=["scheduler"])
async def create_agent_trigger(
    request: CreateTriggerRequest,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """
    Create a Mage trigger for an existing agent.
    Used for agents that were created when Mage was down,
    or old agents that don't have triggers yet.
    """
    logger.info(
        f"[TRIGGER] Creating trigger for agent: {request.agent_name} ({request.agent_id})"
    )

    agent_meta = {
        "agent_name": request.agent_name,
        "agent_model": request.agent_model,
        "agent_description": request.agent_description,
        "owner_id": current_user.email,
    }

    try:
        result = process_agent_registration(
            agent_id=request.agent_id,
            agent_meta=agent_meta,
            create_initial_run=False,
        )

        if result:
            logger.info(
                f"[TRIGGER] Successfully created trigger for {request.agent_name}"
            )
            return {
                "success": True,
                "schedule_id": result.get("schedule_id"),
                "trigger_token": result.get("trigger_token"),
                "agent_name": request.agent_name,
                "status": result.get("status"),
            }
        else:
            raise HTTPException(
                status_code=500,
                detail="Trigger creation returned None. Is Mage AI running?",
            )

    except Exception as e:
        logger.error(f"[TRIGGER] Failed to create trigger: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/pipelines/{pipeline_uuid}/schedules", tags=["scheduler"])
async def list_pipeline_schedules(
    pipeline_uuid: str,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """List triggers/schedules for a pipeline, filtered to only the current user's agents."""
    all_schedules = scheduler_client.get_pipeline_schedules(pipeline_uuid)
    user_agent_ids = _get_user_agent_ids(current_user.email)
    return [s for s in all_schedules if s.get("name") in user_agent_ids]


@router.get(
    "/pipelines/{pipeline_uuid}/schedules/{schedule_id}/runs", tags=["scheduler"]
)
async def list_schedule_runs(
    pipeline_uuid: str,
    schedule_id: str,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """List runs for a specific schedule, after verifying it belongs to the user."""
    # Verify the schedule belongs to one of the user's agents
    all_schedules = scheduler_client.get_pipeline_schedules(pipeline_uuid)
    user_agent_ids = _get_user_agent_ids(current_user.email)
    # Compare as strings: Mage schedule ids are ints, th2etl uses the scheduler
    # name (e.g. "1201") as the id. Coercing the path param to int would make a
    # th2etl id like "1201" never match, returning a spurious 403.
    schedule = next((s for s in all_schedules if str(s.get("id")) == str(schedule_id)), None)
    if not schedule or schedule.get("name") not in user_agent_ids:
        raise HTTPException(
            status_code=403, detail="This schedule does not belong to your agents."
        )
    return scheduler_client.get_pipeline_runs(schedule_id)


@router.put("/pipelines/runs/{run_id}/cancel", tags=["scheduler"])
async def cancel_pipeline_run(
    run_id: int,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Cancel a pipeline run."""
    result = scheduler_client.cancel_pipeline_run(run_id)
    if result is None:
        raise HTTPException(status_code=500, detail="Failed to cancel pipeline run")
    return result


@router.get("/pipelines/runs/{run_id}", tags=["scheduler"])
async def get_pipeline_run(
    run_id: int,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Get details for a specific pipeline run."""
    result = scheduler_client.get_pipeline_run(run_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Pipeline run not found")
    return result


@router.get("/pipelines/runs/{run_id}/logs", tags=["scheduler"])
async def get_pipeline_run_logs(
    run_id: int,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Structured, step-by-step log for a run, so the Orchestrator UI can show a
    run's execution narrative instead of just its status. Backed by th2etl's
    ``/runs/{id}/logs``; an empty list under the default Mage orchestrator."""
    logs = scheduler_client.get_pipeline_run_logs(run_id)
    if not logs:
        # Nothing to leak (Mage no-op, or a run with no captured log): skip the
        # extra upstream call and the ownership check.
        return []
    # Ownership guard: the execution log carries the run's full narrative (which
    # may include business content), so — unlike a bare run lookup — verify the
    # run belongs to one of the caller's agents before returning it. The owning
    # agent is the run's scheduler_name (th2etl) / schedule id.
    run = scheduler_client.get_pipeline_run(run_id)
    owner = run.get("scheduler_name") or run.get("pipeline_schedule_id") if isinstance(run, dict) else None
    user_agent_ids = _get_user_agent_ids(current_user.email)
    if owner is None or str(owner) not in user_agent_ids:
        raise HTTPException(
            status_code=403, detail="This run does not belong to your agents."
        )
    return logs


class UpdateScheduleRequest(BaseModel):
    status: str | None = None
    schedule_interval: str | None = None
    start_time: str | None = None


@router.put("/pipelines/{pipeline_uuid}/schedules/{schedule_id}", tags=["scheduler"])
async def update_pipeline_schedule(
    pipeline_uuid: str,
    schedule_id: str,
    request: UpdateScheduleRequest,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Update a pipeline schedule (status, interval, start_time)."""
    # Verify the schedule belongs to one of the user's agents
    all_schedules = scheduler_client.get_pipeline_schedules(pipeline_uuid)
    user_agent_ids = _get_user_agent_ids(current_user.email)
    # Compare as strings: Mage schedule ids are ints, th2etl uses the scheduler
    # name (e.g. "1201") as the id. Coercing the path param to int would make a
    # th2etl id like "1201" never match, returning a spurious 403.
    schedule = next((s for s in all_schedules if str(s.get("id")) == str(schedule_id)), None)
    if not schedule or schedule.get("name") not in user_agent_ids:
        raise HTTPException(
            status_code=403, detail="This schedule does not belong to your agents."
        )

    result = scheduler_client.update_schedule(
        schedule_id=schedule_id,
        schedule_interval=request.schedule_interval,
        start_time=request.start_time,
        status=request.status,
    )
    if result is None:
        raise HTTPException(status_code=500, detail="Failed to update schedule")
    return result

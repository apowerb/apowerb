from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from apowerb.auth.dependencies import get_current_user
from apowerb.users import schemas as user_schemas
from apowerb.scheduler.mage import get_orchestrator, process_agent_registration
from apowerb.scheduler.th2etl_client import OrchestratorUnavailable
from apowerb.core.agent_main import fetch_agents
from apowerb.configs.settings import get_settings
from apowerb.configs.th2logger import setup_logging

logger = setup_logging(__name__)

router = APIRouter()

# Everything that belongs to each orchestrator, not just its URL.
#
# Reading the URL alone had it backwards. An operator who installs Mage on the
# documented default port has no reason to touch `BASE_URL` -- it already
# matches his deployment -- but `scheduler/mage.py` refuses to build a client
# without `API_KEY`, so that is the field he does set. He would have been
# classified as having configured nothing, and a real outage of his Mage
# logged at DEBUG under a message telling him no orchestrator was configured.
#
# So the question is not "did he name the address" but "did he engage with
# this orchestrator at all". Any one of these says yes.
_ORCHESTRATOR_SETTINGS = {
    "mage": (
        "base_url",
        "api_key",
        "oauth_token",
        "project_name",
        "mage_pipeline_uuid",
    ),
    "th2etl": ("th2etl_base_url", "th2etl_api_key"),
}

_NOT_CONFIGURED = (
    "No orchestrator is configured in this installation. Scheduled runs need "
    "one (Mage or th2etl); set its URL, or set SCHEDULER_ENABLED=false to drop "
    "the feature and its screen entirely."
)


def _orchestrator_is_configured(settings) -> bool:
    """Did this installation ever name an orchestrator?

    `model_fields_set` holds what the environment actually provided -- the
    only way to tell "left at the shipped default" from "typed in" once the
    settings object is built. Any single setting of the selected orchestrator
    counts: the question is whether somebody engaged with it, not whether he
    happened to name its address.

    ⚠️ On its own this does not mean "no orchestrator here": one genuinely
    running beside the API on `localhost:6789` needs no configuration at all,
    and an install like that is perfectly sound. What carries the meaning is
    the **conjunction** at the only call site -- nothing configured *and* the
    call just failed. Either half alone says nothing, which is also why this
    must not become a startup warning: at boot the second half is unknown.

    The asymmetry is deliberate. A deployment that sets `API_KEY` for some
    unrelated reason is read as having a Mage, and a genuine absence then
    logs at ERROR -- noise. The opposite mistake silences a real outage, which
    is the failure this whole change exists to prevent. When in doubt, be
    loud.

    Reads the settings of the orchestrator actually **selected**: a deployment
    switched to th2etl that only ever configured Mage engaged with nothing the
    client will use.
    """
    names = _ORCHESTRATOR_SETTINGS.get(
        str(settings.orchestrator).strip().lower(), _ORCHESTRATOR_SETTINGS["mage"]
    )
    return bool(set(names) & settings.model_fields_set)


def _orchestrator_outage(e: OrchestratorUnavailable, doing: str) -> HTTPException:
    """The one answer every route in this file gives to an unreachable
    orchestrator.

    503 and never 500, never an empty result: the screen must be able to say
    "the orchestrator is down" instead of "you have no pipelines" -- and a 500
    reads as a broken server, when what happened is that an optional component
    is not answering. Returning an empty list is worse still: a dead
    orchestrator then renders as a perfectly healthy, empty dashboard, which is
    how one outage went three weeks unnoticed (see ``OrchestratorUnavailable``).

    Two situations wear the same exception and must not wear the same log
    level. An orchestrator nobody installed is not one that is down: a
    deployment carrying no Mage logged ERROR on every visit to the screen,
    quoting a localhost address its operator never typed -- and an ERROR
    meaning "you did not install an optional component" teaches whoever reads
    the logs to scroll past ERROR lines. So DEBUG plus a sentence saying how to
    turn the feature off, against ERROR plus the real cause.

    Per route rather than one handler on the app: this router is mounted by
    consumers on their own FastAPI app (see ``_OrchestratorClientProxy``), and
    a handler registered in ``main.py`` would leave every one of them with the
    500s this replaces. It would also capture callers outside this file that
    map the same exception deliberately -- the BI refresh route answers 500 on
    purpose -- turning their choice into this one behind their back.
    """
    if not _orchestrator_is_configured(get_settings()):
        logger.debug("no orchestrator configured while %s; %s", doing, e)
        return HTTPException(status_code=503, detail=_NOT_CONFIGURED)
    logger.error("orchestrator unreachable while %s: %s", doing, e)
    return HTTPException(status_code=503, detail=str(e))


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
        raise _orchestrator_outage(e, "listing pipelines") from e


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

    except OrchestratorUnavailable as e:
        # Ordered before the catch-all below, which would otherwise answer 500
        # with the exception text -- telling operators to debug the agent they
        # just registered, when what is down is their scheduler.
        raise _orchestrator_outage(e, "registering an agent") from e
    except Exception as e:
        logger.error(f"[TRIGGER] Failed to create trigger: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/pipelines/{pipeline_uuid}/schedules", tags=["scheduler"])
async def list_pipeline_schedules(
    pipeline_uuid: str,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """List triggers/schedules for a pipeline, filtered to only the current user's agents."""
    try:
        all_schedules = scheduler_client.get_pipeline_schedules(pipeline_uuid)
    except OrchestratorUnavailable as e:
        raise _orchestrator_outage(e, "listing schedules") from e
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
    # Verify the schedule belongs to one of the user's agents.
    #
    # This call has to answer before the ownership check can mean anything: an
    # unreachable orchestrator returns no schedules, so every schedule looks
    # like somebody else's and the caller is told 403 -- their own schedule
    # denied to them, in the name of a check that never ran.
    try:
        all_schedules = scheduler_client.get_pipeline_schedules(pipeline_uuid)
    except OrchestratorUnavailable as e:
        raise _orchestrator_outage(e, "listing schedules") from e
    user_agent_ids = _get_user_agent_ids(current_user.email)
    # Compare as strings: Mage schedule ids are ints, th2etl uses the scheduler
    # name (e.g. "1201") as the id. Coercing the path param to int would make a
    # th2etl id like "1201" never match, returning a spurious 403.
    schedule = next((s for s in all_schedules if str(s.get("id")) == str(schedule_id)), None)
    if not schedule or schedule.get("name") not in user_agent_ids:
        raise HTTPException(
            status_code=403, detail="This schedule does not belong to your agents."
        )
    try:
        return scheduler_client.get_pipeline_runs(schedule_id)
    except OrchestratorUnavailable as e:
        raise _orchestrator_outage(e, "listing runs") from e


@router.put("/pipelines/runs/{run_id}/cancel", tags=["scheduler"])
async def cancel_pipeline_run(
    run_id: int,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Cancel a pipeline run."""
    try:
        result = scheduler_client.cancel_pipeline_run(run_id)
    except OrchestratorUnavailable as e:
        raise _orchestrator_outage(e, "cancelling a run") from e
    if result is None:
        raise HTTPException(status_code=500, detail="Failed to cancel pipeline run")
    return result


@router.get("/pipelines/runs/{run_id}", tags=["scheduler"])
async def get_pipeline_run(
    run_id: int,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Get details for a specific pipeline run."""
    try:
        result = scheduler_client.get_pipeline_run(run_id)
    except OrchestratorUnavailable as e:
        # Not a 404: "this run does not exist" is a claim about the run, and we
        # never got to ask. The client keeps returning None for a run the
        # orchestrator genuinely answered about, and that still means 404.
        raise _orchestrator_outage(e, "reading a run") from e
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
    try:
        logs = scheduler_client.get_pipeline_run_logs(run_id)
    except OrchestratorUnavailable as e:
        raise _orchestrator_outage(e, "reading a run log") from e
    if not logs:
        # Nothing to leak (Mage no-op, or a run with no captured log): skip the
        # extra upstream call and the ownership check.
        return []
    # Ownership guard: the execution log carries the run's full narrative (which
    # may include business content), so — unlike a bare run lookup — verify the
    # run belongs to one of the caller's agents before returning it. The owning
    # agent is the run's scheduler_name (th2etl) / schedule id.
    try:
        run = scheduler_client.get_pipeline_run(run_id)
    except OrchestratorUnavailable as e:
        raise _orchestrator_outage(e, "reading a run") from e
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
    # Verify the schedule belongs to one of the user's agents.
    #
    # As in ``list_schedule_runs``, an orchestrator that cannot answer would
    # make this check deny callers their own schedule with a 403.
    try:
        all_schedules = scheduler_client.get_pipeline_schedules(pipeline_uuid)
    except OrchestratorUnavailable as e:
        raise _orchestrator_outage(e, "listing schedules") from e
    user_agent_ids = _get_user_agent_ids(current_user.email)
    # Compare as strings: Mage schedule ids are ints, th2etl uses the scheduler
    # name (e.g. "1201") as the id. Coercing the path param to int would make a
    # th2etl id like "1201" never match, returning a spurious 403.
    schedule = next((s for s in all_schedules if str(s.get("id")) == str(schedule_id)), None)
    if not schedule or schedule.get("name") not in user_agent_ids:
        raise HTTPException(
            status_code=403, detail="This schedule does not belong to your agents."
        )

    # Deliberately not wrapped: ``update_schedule`` is shared with five
    # callers outside this router, so making it report an outage is a change
    # with its own blast radius and its own measurements to take. The check
    # above already answers 503 for an orchestrator that is down, and only an
    # orchestrator that dies between these two calls still reaches the 500
    # below -- a narrower hole than the one this commit closes, left open on
    # purpose rather than by omission.
    result = scheduler_client.update_schedule(
        schedule_id=schedule_id,
        schedule_interval=request.schedule_interval,
        start_time=request.start_time,
        status=request.status,
    )
    if result is None:
        raise HTTPException(status_code=500, detail="Failed to update schedule")
    return result

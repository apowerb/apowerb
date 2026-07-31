import os

import aiohttp
from fastapi import APIRouter, HTTPException, Depends, Header
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from typing import Any, Optional

_security = HTTPBearer(auto_error=False)

from apowerb.core.adk_runner import (
    run_adk_agent,
    stream_adk_agent,
    list_adk_sessions,
    get_adk_session,
    parse_session_to_trace,
    update_adk_agent_session,
    create_adk_agent_session,
    delete_adk_agent_session,
)
from apowerb.core.invocation_context import set_current_invoker
from apowerb.schema.adk_runner_schema import (
    RunADKAgentRequest,
    UpdateADKAgentSessionRequest,
    CreateADKAgentSessionRequest,
    GenerateTitleRequest,
    NewMessage,
)
from apowerb.helpers.title_generator import generate_session_title
from apowerb.core.agent_main import get_agent_folder_name, fetch_agents
from apowerb.auth.dependencies import get_current_user
from apowerb.users import schemas as user_schemas
from apowerb.scheduler.run_agent_background import (
    run_agent_from_jwt,
    run_agent_from_refresh_token,
    schedule_agent_run,
)
from datetime import timedelta
from logging import getLogger
from pydantic import BaseModel

from apowerb.helpers.security import create_access_token
from apowerb.helpers.ownership import enforce_user_id_match as _enforce_user_id_match
from apowerb.helpers import notify_etl

logger = getLogger(__name__)
router = APIRouter()


def _internal_token(current_user) -> str:
    """Generate a short-lived JWT for internal ADK calls that pass through ADKAuthMiddleware."""
    return create_access_token(
        data={"sub": current_user.email, "type": "access"},
        expires_delta=timedelta(minutes=10),
    )

class ScheduleAgentRunRequest(BaseModel):
    """Request schema for scheduling an agent run."""
    agent_id: str  # Agent identifier (e.g., "agent95")
    user_id: str
    session_id: str
    new_message: dict
    run_mode: str = "single"
    streaming: bool = False
    agent_metadata: dict | None = None
    schedule_interval: str = "@hourly"  # NEW: Cron or preset (@hourly, @daily, @weekly, @monthly)
    start_time: Optional[str] = None

class RunAgentNowRequest(BaseModel):
    agent_id: str
    user_id: str | None = None
    message: str = "Run agent"

@router.post("/run", tags=["adk"])
async def run_agent(
    request: RunADKAgentRequest,
    credentials: HTTPAuthorizationCredentials = Depends(_security),
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Endpoint to run an ADK agent with comprehensive error handling."""
    _enforce_user_id_match(request.user_id, current_user)

    # User-personal tools (Outlook, Gmail, Drive, ...) resolve their
    # integrations against this invoker, not against AGENT_OWNER.
    set_current_invoker(current_user.email)

    logger.info(
        f"[ADK RUN] Starting agent run: agent={request.agent_name}, user={request.user_id}, session={request.session_id}"
    )

    # Inject dashboard context from session_id or data
    _dash_id = (request.data or {}).get("dashboard_id")
    if not _dash_id and request.session_id.startswith("dashboard-chat-"):
        _dash_id = request.session_id.replace("dashboard-chat-", "")
    if _dash_id:
        os.environ["AGENT_DASHBOARD_ID"] = str(_dash_id)
    # Per-conversation chart dashboard: BI tools read this to scope the
    # "Charts du chat" dashboard to the CURRENT chat, so charts from different
    # conversations no longer pile into one shared board.
    os.environ["AGENT_CHAT_SESSION_ID"] = request.session_id or ""

    # Validate request parameters
    if not request.agent_name or not request.agent_name.strip():
        logger.error("[ADK RUN] Missing or empty agent_name")
        raise HTTPException(status_code=400, detail="agent_name is required")
    
    if not request.user_id or not request.user_id.strip():
        logger.error("[ADK RUN] Missing or empty user_id")
        raise HTTPException(status_code=400, detail="user_id is required")
    
    if not request.session_id or not request.session_id.strip():
        logger.error("[ADK RUN] Missing or empty session_id")
        raise HTTPException(status_code=400, detail="session_id is required")
    
    if not request.new_message:
        logger.error("[ADK RUN] Missing new_message")
        raise HTTPException(status_code=400, detail="new_message is required")

    # Validate message format
    try:
        NewMessage(**request.new_message)
    except Exception as e:
        logger.error(f"[ADK RUN] Invalid message format: {str(e)}")
        raise HTTPException(
            status_code=400, 
            detail=f"Invalid message format: {str(e)}"
        )

    # Resolve agent name to folder name
    try:
        folder_name = get_agent_folder_name(request.agent_name)
        logger.info(f"[ADK RUN] Resolved agent name {request.agent_name} to {folder_name}")
    except ValueError as e:
        logger.error(f"[ADK RUN] Agent not found: {request.agent_name}")
        raise HTTPException(
            status_code=404,
            detail=f"Agent not found: {request.agent_name}"
        )
    except Exception as e:
        logger.error(f"[ADK RUN] Error resolving agent name: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Error resolving agent: {str(e)}"
        )

    # Quota du modele mutualise -- avant toute execution, et avant meme de
    # creer la session : un run refuse ne doit rien laisser derriere lui.
    from apowerb.core.run_gate import apply_run_guards

    await apply_run_guards(
        agent_name=folder_name,
        owner_id=current_user.email,
        plan=current_user.plan,
    )

    # Check if session exists, create if not
    session_was_created = False
    user_token = credentials.credentials if credentials else _internal_token(current_user)
    try:
        logger.info(f"[ADK RUN] Checking if session exists: {request.session_id}")
        try:
            await get_adk_session(
                agent_name=folder_name,
                user_id=request.user_id,
                session_id=request.session_id,
                token=user_token,
            )
            logger.info(f"[ADK RUN] Session {request.session_id} exists")
            session_was_created = False
        except Exception:
            # Session doesn't exist, create it
            logger.info(f"[ADK RUN] Session {request.session_id} not found, creating it")
            await create_adk_agent_session(
                agent_name=folder_name,
                user_id=request.user_id,
                session_id=request.session_id,
                data={},
                token=user_token,
            )
            logger.info(f"[ADK RUN] Successfully created session {request.session_id}")
            session_was_created = True
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[ADK RUN] Error handling session: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Error creating/checking session: {str(e)}"
        )

    # Run the agent
    try:
        logger.info(f"[ADK RUN] Calling run_adk_agent for {folder_name}")
        result = await run_adk_agent(
            agent_name=folder_name,
            user_id=request.user_id,
            session_id=request.session_id,
            run_mode=request.run_mode,
            new_message=request.new_message,
            streaming=request.streaming,
            token=credentials.credentials if credentials else None,
        )
        logger.info(f"[ADK RUN] Successfully completed agent run: {request.agent_name}")
        
        # Add session creation info to result
        if isinstance(result, dict):
            result["session_created"] = session_was_created
        
        return result
    except HTTPException:
        # Re-raise HTTP exceptions as-is
        raise
    except ValueError as e:
        logger.error(f"[ADK RUN] Validation error: {str(e)}", exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))
    except ConnectionError as e:
        logger.error(f"[ADK RUN] Connection error: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=503,
            detail=f"Service temporarily unavailable: {str(e)}"
        )
    except TimeoutError as e:
        logger.error(f"[ADK RUN] Timeout error: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=504,
            detail=f"Request timeout: {str(e)}"
        )
    except Exception as e:
        logger.error(
            f"[ADK RUN] Unexpected error running agent {request.agent_name}: {str(e)}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail=f"Internal server error: {str(e)}"
        )


@router.post("/run_sse", tags=["adk"])
async def run_agent_sse(
    request: RunADKAgentRequest,
    credentials: HTTPAuthorizationCredentials = Depends(_security),
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Endpoint to run an ADK agent with SSE streaming."""
    _enforce_user_id_match(request.user_id, current_user)

    # User-personal tools (Outlook, Gmail, Drive, ...) resolve their
    # integrations against this invoker, not against AGENT_OWNER.
    set_current_invoker(current_user.email)

    logger.info(
        f"[ADK SSE] Starting streaming agent run: agent={request.agent_name}, user={request.user_id}, session={request.session_id}"
    )

    # Inject dashboard context from session_id or data
    _dash_id = (request.data or {}).get("dashboard_id")
    if not _dash_id and request.session_id.startswith("dashboard-chat-"):
        _dash_id = request.session_id.replace("dashboard-chat-", "")
    if _dash_id:
        os.environ["AGENT_DASHBOARD_ID"] = str(_dash_id)
        logger.info(f"[ADK SSE] Dashboard context set: {_dash_id}")
    # Per-conversation chart dashboard: BI tools read this to scope the
    # "Charts du chat" dashboard to the CURRENT chat, so charts from different
    # conversations no longer pile into one shared board.
    os.environ["AGENT_CHAT_SESSION_ID"] = request.session_id or ""

    # Resolve agent name to folder name
    folder_name = get_agent_folder_name(request.agent_name)
    logger.info(f"[ADK SSE] Resolved agent name {request.agent_name} to {folder_name}")

    # Quota du modele mutualise : on refuse AVANT d'ouvrir le flux. Couper
    # un SSE en cours donnerait une reponse tronquee et un tour a moitie
    # facture ; ici l'utilisateur recoit un 402 propre, exploitable.
    # Import local : les imports de ce module sont deja tous post-`_security`
    # (E402), inutile d'en ajouter un de plus.
    from apowerb.core.run_gate import apply_run_guards

    await apply_run_guards(
        agent_name=folder_name,
        owner_id=current_user.email,
        plan=current_user.plan,
    )

    try:
        NewMessage(**request.new_message)

        return StreamingResponse(
            stream_adk_agent(
                agent_name=folder_name,
                user_id=request.user_id,
                session_id=request.session_id,
                new_message=request.new_message,
                streaming=request.streaming,
                token=credentials.credentials if credentials else None,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    except Exception as e:
        logger.error(
            f"[ADK SSE] Error starting stream for agent {request.agent_name}: {str(e)}",
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/sessions/list", tags=["adk"])
async def list_all_sessions(
    current_user: user_schemas.User = Depends(get_current_user),
):
    """List all ADK sessions across all agents for the current user."""
    import asyncio

    user_id = current_user.email
    try:
        agents = fetch_agents(user_id=user_id)
    except Exception as e:
        logger.error(f"[ADK] Error fetching agents for session list: {e}")
        agents = []

    all_sessions = []

    token = _internal_token(current_user)

    async def fetch_for_agent(agent):
        agent_id = agent.get("agent_id")
        agent_name = agent.get("agent_name", "")
        folder_name = f"agent{agent_id}"
        try:
            sessions = await list_adk_sessions(
                agent_name=folder_name,
                user_id=user_id,
                token=token,
            )
            for s in sessions:
                s["agent_name"] = agent_name
                s["agent_folder"] = folder_name
            return sessions
        except Exception as e:
            logger.warning(f"[ADK] Failed to list sessions for {folder_name}: {e}")
            return []

    results = await asyncio.gather(
        *[fetch_for_agent(a) for a in agents],
        return_exceptions=True,
    )

    for result in results:
        if isinstance(result, list):
            all_sessions.extend(result)

    # Sort by update_time descending
    all_sessions.sort(
        key=lambda s: s.get("update_time") or s.get("create_time") or 0,
        reverse=True,
    )

    return {"sessions": all_sessions}


@router.post("/generate_title", tags=["adk"])
async def generate_title_endpoint(
    request: GenerateTitleRequest,
    current_user: user_schemas.User = Depends(get_current_user),
) -> dict[str, str]:
    """Génère un titre court de conversation à partir de son premier message.

    Best-effort : renvoie toujours un titre (fallback déterministe si le LLM est
    indisponible) pour ne jamais bloquer le front."""
    title = await generate_session_title(request.message, request.agent_id)
    return {"title": title}


@router.post("/sessions", tags=["adk"])
async def create_session(
    request: CreateADKAgentSessionRequest,
    credentials: HTTPAuthorizationCredentials = Depends(_security),
    current_user: user_schemas.User = Depends(get_current_user),
) -> dict[str, Any]:
    """Endpoint to create an ADK agent session."""
    _enforce_user_id_match(request.user_id, current_user)
    try:
        logger.info("start creating adk session")
        folder_name = get_agent_folder_name(request.agent_name)
        result = await create_adk_agent_session(
            agent_name=folder_name,
            user_id=request.user_id,
            session_id=request.session_id,
            data=request.data,
            token=credentials.credentials if credentials else None,
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/sessions/{agent_name}/{user_id}/{session_id}", tags=["adk"])
async def get_session_history(
    agent_name: str,
    user_id: str,
    session_id: str,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Retrieve message history from an ADK session."""
    _enforce_user_id_match(user_id, current_user)
    try:
        folder_name = get_agent_folder_name(agent_name)
        token = _internal_token(current_user)
        session_data = await get_adk_session(
            agent_name=folder_name,
            user_id=user_id,
            session_id=session_id,
            token=token,
        )

        messages = []
        for event in session_data.get("events", []):
            content = event.get("content")
            if not content or not content.get("parts"):
                continue
            text_parts = []
            thinking_parts = []
            for p in content["parts"]:
                if p.get("thought") and p.get("text"):
                    thinking_parts.append(p["text"])
                elif p.get("text"):
                    text_parts.append(p["text"])
            text = "".join(text_parts)
            thinking = "".join(thinking_parts)
            if not text.strip() and not thinking.strip():
                continue
            role = "user" if event.get("author") == "user" else "assistant"
            msg = {
                "role": role,
                "content": text,
                "timestamp": event.get("timestamp"),
            }
            if thinking:
                msg["thinking"] = thinking
            messages.append(msg)

        return {"messages": messages}
    except aiohttp.ClientResponseError as e:
        # Session (or agent) not yet materialised in ADK — surface a 404
        # so the frontend can react by creating the session. Log at warning
        # level only; this is a normal "first touch" flow, not an error.
        if e.status == 404:
            logger.warning(
                "[ADK] Session not found (agent=%s user=%s session=%s)",
                agent_name, user_id, session_id,
            )
            raise HTTPException(status_code=404, detail="Session not found")
        logger.error(f"[ADK] ADK responded with {e.status}: {e.message}")
        raise HTTPException(status_code=e.status, detail=e.message)
    except Exception as e:
        logger.error(f"[ADK] Error fetching session history: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/sessions/{agent_name}/{user_id}/{session_id}/trace", tags=["adk"])
async def get_session_trace(
    agent_name: str,
    user_id: str,
    session_id: str,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Retrieve a structured trace of an ADK session for supervision."""
    _enforce_user_id_match(user_id, current_user)
    try:
        folder_name = get_agent_folder_name(agent_name)
        token = _internal_token(current_user)
        session_data = await get_adk_session(
            agent_name=folder_name,
            user_id=user_id,
            session_id=session_id,
            token=token,
        )
        trace = parse_session_to_trace(session_data, agent_name)
        return trace
    except Exception as e:
        logger.error(f"[ADK] Error fetching session trace: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/sessions/{agent_name}/{user_id}/{session_id}", tags=["adk"])
async def update_session(
    agent_name: str,
    user_id: str,
    session_id: str,
    request: UpdateADKAgentSessionRequest,
    credentials: HTTPAuthorizationCredentials = Depends(_security),
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Endpoint to update an ADK agent session."""
    _enforce_user_id_match(user_id, current_user)
    try:
        folder_name = get_agent_folder_name(agent_name)
        result = await update_adk_agent_session(
            agent_name=folder_name,
            user_id=user_id,
            session_id=session_id,
            data=request.data,
            token=credentials.credentials if credentials else None,
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/sessions/{agent_name}/{user_id}/{session_id}", tags=["adk"])
async def delete_session(
    agent_name: str,
    user_id: str,
    session_id: str,
    credentials: HTTPAuthorizationCredentials = Depends(_security),
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Endpoint to delete an ADK agent session."""
    _enforce_user_id_match(user_id, current_user)
    try:
        folder_name = get_agent_folder_name(agent_name)
        result = await delete_adk_agent_session(
            agent_name=folder_name,
            user_id=user_id,
            session_id=session_id,
            token=credentials.credentials if credentials else None,
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class RunFromJWTRequest(BaseModel):
    agent_id: str | None = None  # Sent by Mage block — used for token rotation
    data: dict | None = None     # agent_meta, ignored here


@router.post("/run_from_jwt", tags=["adk"])  # endpoint is called by Mage AI to actually execute the agent
async def run_agent_from_jwt_endpoint(
    request: RunFromJWTRequest,
    authorization: str = Header(..., description="Bearer JWT token"),
):
    """
    Execute an agent using JWT token (called by Mage AI).
    Automatically rotates the token in the trigger after each successful run
    so the jwt_token stored in Mage never expires (self-healing schedule).
    """
    logger.info(
        f"[JWT RUN] Received agent execution request from Mage "
        f"(agent_id={request.agent_id})"
    )

    try:
        # Extract JWT token from Bearer header
        if not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=401,
                detail="Invalid Authorization header format. Expected: Bearer <token>",
            )

        jwt_token = authorization.replace("Bearer ", "").strip()

        if not jwt_token:
            raise HTTPException(status_code=401, detail="Missing JWT token")

        # Use run_agent_from_refresh_token so the token is rotated after
        # every successful execution. The new 90-day token overwrites the
        # one stored in the Mage trigger variables, meaning the schedule
        # never hits a token-expiry failure as long as it runs at least
        # once every 90 days.
        result = await run_agent_from_refresh_token(
            agent_refresh_token=jwt_token,
            agent_id=request.agent_id,   # None-safe: rotation is skipped if missing
            agent_meta=request.data,     # Preserved on rotation so agent_meta is never lost
        )

        logger.info(
            f"[JWT RUN] Successfully executed agent: {result.get('agent_name')}, "
            f"token_rotated={result.get('token_rotated', False)}"
        )
        return result

    except ValueError as e:
        logger.error(f"[JWT RUN] Validation error: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"[JWT RUN] Error: {str(e)}", exc_info=True)
        # Alert on failure of the live scheduled/Run-Now execution path. This is
        # the path th2etl runs hit; the legacy run_agent_from_jwt already alerts,
        # this one did not. notify_job_failure is throttled (15 min per job+error
        # signature) so bloc retries don't spam, and never raises.
        await notify_etl.notify_job_failure(
            "agent-run", f"{type(e).__name__}: {e}", context=str(request.agent_id)
        )
        raise HTTPException(status_code=500, detail="Agent execution failed")


@router.post("/schedule_run", tags=["adk"])
async def schedule_agent_run_endpoint(
    request: ScheduleAgentRunRequest,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """
    Schedule an agent to run via Mage AI.
    
    - Creates a SCHEDULE trigger (time-based) if it doesn't exist
    - Supports cron expressions and presets (@hourly, @daily, @weekly, @monthly)
    - Lazily creates triggers on first schedule (not during agent creation)
    
    Request body:
    {
        "agent_id": "agent95",
        "user_id": "user@example.com",
        "session_id": "session_abc",
        "new_message": {"content": "Hello", "role": "user"},
        "run_mode": "single",  # optional
        "streaming": false,     # optional
        "agent_metadata": {},    # optional
        "schedule_interval": "@hourly",  # NEW: @hourly, @daily, @weekly, @monthly, or cron
        "start_time": "2026-02-13T10:30:00" 
    }
    
    Returns:
        Schedule information including run_id and status
    """
    _enforce_user_id_match(request.user_id, current_user)

    logger.info(f"[SCHEDULE] Scheduling agent: {request.agent_id} with interval: {request.schedule_interval}")

    try:
        result = await schedule_agent_run(
            agent_id=request.agent_id,
            user_id=request.user_id,
            session_id=request.session_id,
            new_message=request.new_message,
            run_mode=request.run_mode,
            streaming=request.streaming,
            agent_metadata=request.agent_metadata,
            schedule_interval=request.schedule_interval,  # NEW: Pass schedule interval
            start_time=request.start_time,
        )
        
        logger.info(f"[SCHEDULE] Successfully scheduled: run_id={result.get('run_id')}")
        return result
        
    except Exception as e:
        logger.error(f"[SCHEDULE] Error scheduling agent run: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/run_now", tags=["adk"])
async def run_agent_now_endpoint(
    request: RunAgentNowRequest,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """
    Trigger an immediate Mage pipeline run for an agent.

    If the agent doesn't have a schedule trigger yet, one is created first
    with a default @daily interval (inactive), then a run is triggered.
    """
    from apowerb.scheduler.run_agent_background import get_agent_by_id, get_orchestrator, create_agent_run_token
    from apowerb.core.agent_main import get_agent_folder_name

    _enforce_user_id_match(request.user_id, current_user)
    user_id = request.user_id or current_user.email
    agent_id = request.agent_id

    logger.info(f"[RUN NOW] Triggering immediate run for agent: {agent_id}")

    try:
        # 1. Validate agent exists
        agent_info = get_agent_by_id(agent_id, user_id)
        if not agent_info:
            raise HTTPException(status_code=404, detail=f"Agent not found: {agent_id}")

        agent_name = agent_info.get("agent_name")

        # 2. Find existing schedule trigger
        orchestrator = get_orchestrator()
        schedules = orchestrator.client.get_pipeline_schedules(orchestrator.PIPELINE_UUID)
        existing_trigger = next(
            (s for s in schedules if s.get("name") == agent_id), None
        )

        schedule_id = None

        if existing_trigger:
            schedule_id = existing_trigger.get("id")
            logger.info(f"[RUN NOW] Found existing schedule: {schedule_id}")
        else:
            # Create a schedule trigger (inactive by default for run-now-only agents)
            logger.info(f"[RUN NOW] No schedule found, creating one for agent: {agent_id}")
            agent_meta = {
                "agent_name": agent_name,
                "agent_model": agent_info.get("agent_model"),
                "agent_description": agent_info.get("agent_description"),
                "owner_id": agent_info.get("owner_id"),
            }
            trigger_result = orchestrator.create_schedule_trigger_for_agent(
                agent_id=agent_id,
                agent_meta=agent_meta,
                schedule_interval="@daily",
            )
            if not trigger_result:
                raise HTTPException(status_code=500, detail="Failed to create schedule trigger")
            schedule_id = trigger_result.get("schedule_id")
            # Set to inactive since this was auto-created just for run-now
            orchestrator.client.update_schedule(schedule_id=schedule_id, status="inactive")
            logger.info(f"[RUN NOW] Created inactive schedule: {schedule_id}")

        # 3. Create JWT token for the run
        import time as _time
        session_id = f"run_now_{agent_id}_{int(_time.time())}"
        folder_name = get_agent_folder_name(agent_name)
        new_message = {"role": "user", "parts": [{"text": request.message}]}

        jwt_token = create_agent_run_token(
            agent_name=agent_name,
            user_id=user_id,
            session_id=session_id,
            new_message=new_message,
            run_mode="single",
            streaming=False,
        )

        # 4. Trigger immediate pipeline run
        run_result = orchestrator.client.trigger_pipeline_run_for_schedule(
            schedule_id=schedule_id,
            run_variables={"jwt_token": jwt_token},
        )

        if not run_result:
            raise HTTPException(status_code=500, detail="Failed to trigger pipeline run")

        run_info = run_result.get("pipeline_run", {})
        run_id = run_info.get("id")
        status = run_info.get("status")

        logger.info(f"[RUN NOW] Pipeline run created: run_id={run_id}, status={status}")

        return {
            "success": True,
            "run_id": run_id,
            "status": status,
            "agent_id": agent_id,
            "agent_name": agent_name,
            "schedule_id": schedule_id,
            "session_id": session_id,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[RUN NOW] Error: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# Add this to your adk_runner.py file

@router.post("/run_from_refresh_token", tags=["adk"])
async def run_agent_from_refresh_token_endpoint(
    authorization: str = Header(..., description="Bearer <agent_refresh_token>"),
    agent_id: str = Header(None, description="Agent ID for token rotation (optional)"),
):
    """
    Execute agent using refresh token with TOKEN ROTATION.
    """
    logger.info("[REFRESH] Received agent execution request from Mage")
    
    try:
        # Extract refresh token from Bearer header
        if not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=401,
                detail="Invalid Authorization header format. Expected: Bearer <token>"
            )
        
        agent_refresh_token = authorization.replace("Bearer ", "").strip()
        
        if not agent_refresh_token:
            raise HTTPException(status_code=401, detail="Missing refresh token")
        
        # Execute agent using refresh token with rotation
        from apowerb.scheduler.run_agent_background import run_agent_from_refresh_token
        
        result = await run_agent_from_refresh_token(
            agent_refresh_token,
            agent_id=agent_id  # 🔄 NEW: Pass agent_id for token rotation
        )
        
        logger.info(
            f"[REFRESH] Successfully executed agent: {result.get('agent_name')}, "
            f"Token rotated: {result.get('token_rotated', False)}"
        )
        return result
        
    except ValueError as e:
        logger.error(f"[REFRESH] Validation error: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"[REFRESH] Error: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))    
"""
Dashboard refresh scheduling — connects dashboards to agents via Mage cron triggers.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Annotated

import requests
from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from apowerb.auth.dependencies import get_current_user
from apowerb.bi.dashboards.service import DashboardNotFoundError, DashboardService
from apowerb.bi.dependencies import get_dashboard_service
from apowerb.configs.th2logger import setup_logging
from apowerb.core.agent_main import fetch_agents, get_agent_by_id
from apowerb.scheduler.mage import MageAPIClient, get_orchestrator
from apowerb.scheduler.run_agent_background import schedule_agent_run
from apowerb.users import schemas as user_schemas

logger = setup_logging(__name__)

router = APIRouter(prefix="/dashboards", tags=["dashboard-refresh"])

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

ServiceDep = Annotated[DashboardService, Depends(get_dashboard_service)]
CurrentUser = Annotated[user_schemas.User, Depends(get_current_user)]


class ScheduleRefreshRequest(BaseModel):
    agent_id: int
    interval: str = Field(
        ...,
        description="Cron expression or preset: @hourly, @daily, @weekly, @monthly",
        examples=["@daily", "0 8 * * *", "@hourly"],
    )
    start_time: datetime | None = None
    message_template: str | None = Field(
        default=None,
        description="Custom message sent to the agent. Default: 'Refresh dashboard {title}'",
    )


class ScheduleRefreshResponse(BaseModel):
    success: bool
    schedule_id: str | None = None
    dashboard_id: str
    agent_id: int
    interval: str
    message: str


class DashboardScheduleInfo(BaseModel):
    schedule_id: str
    agent_id: int
    interval: str
    status: str
    next_run: str | None = None
    created_at: str | None = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/{dashboard_id}/schedule-refresh",
    response_model=ScheduleRefreshResponse,
    summary="Schedule automatic refresh of a dashboard via an agent",
)
async def schedule_dashboard_refresh(
    dashboard_id: str,
    body: ScheduleRefreshRequest,
    user: CurrentUser,
    svc: ServiceDep,
) -> ScheduleRefreshResponse:
    """Create a cron-based schedule that triggers an agent to refresh a dashboard."""

    # 1. Verify dashboard exists
    try:
        dashboard = await svc.get(dashboard_id)
    except DashboardNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Dashboard not found: {dashboard_id}",
        )

    # 2. Verify agent exists
    agent_id_str = str(body.agent_id)
    agent_info = get_agent_by_id(agent_id_str, user.email)
    if not agent_info:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent not found: {body.agent_id}",
        )

    # 3. Build the message
    if body.message_template:
        message_text = body.message_template
    else:
        message_text = (
            f"Refresh the dashboard '{dashboard.title}': "
            "query all data sources and update charts and KPIs with the latest data."
        )

    new_message = {"role": "user", "content": message_text}

    # 4. Prepare start_time
    start_time_str: str | None = None
    if body.start_time:
        start_time_str = body.start_time.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    # 5. Call schedule_agent_run to create the Mage cron trigger
    mage_agent_id = agent_info.get("agent_id", f"agent{body.agent_id}")
    session_id = f"dashboard-refresh-{dashboard_id}-{uuid.uuid4().hex[:8]}"

    try:
        result = await schedule_agent_run(
            agent_id=mage_agent_id,
            user_id=user.email,
            session_id=session_id,
            new_message=new_message,
            schedule_interval=body.interval,
            start_time=start_time_str,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        )
    except Exception as exc:
        logger.error(
            f"[REFRESH] Failed to schedule refresh for dashboard {dashboard_id}: {exc}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create schedule via Mage: {exc}",
        )

    logger.info(
        f"[REFRESH] Scheduled refresh for dashboard {dashboard_id} "
        f"with agent {body.agent_id}, interval={body.interval}"
    )

    return ScheduleRefreshResponse(
        success=True,
        schedule_id=str(result.get("schedule_id")),
        dashboard_id=dashboard_id,
        agent_id=body.agent_id,
        interval=body.interval,
        message=result.get("message", "Dashboard refresh scheduled successfully."),
    )


@router.get(
    "/{dashboard_id}/schedules",
    response_model=list[DashboardScheduleInfo],
    summary="List refresh schedules for a dashboard",
)
async def list_dashboard_schedules(
    dashboard_id: str,
    user: CurrentUser,
    svc: ServiceDep,
) -> list[DashboardScheduleInfo]:
    """List all Mage schedules associated with agents for this dashboard."""

    # Verify dashboard exists
    try:
        await svc.get(dashboard_id)
    except DashboardNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Dashboard not found: {dashboard_id}",
        )

    # Fetch all schedules from Mage for the agents pipeline
    orchestrator = get_orchestrator()
    try:
        all_schedules = orchestrator.client.get_pipeline_schedules(
            orchestrator.PIPELINE_UUID
        )
    except Exception as exc:
        logger.error(f"[REFRESH] Failed to fetch schedules from Mage: {exc}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch schedules from Mage",
        )

    # Filter schedules belonging to the current user's agents AND this dashboard.
    # When schedule_dashboard_refresh creates a trigger, the session_id is set to
    # "dashboard-refresh-{dashboard_id}-{random}".  We use this prefix to narrow
    # results to the requested dashboard instead of returning all user schedules.
    dashboard_prefix = f"dashboard-refresh-{dashboard_id}"

    user_agents = fetch_agents(user.email)
    user_agent_ids: set[str] = set()
    for a in user_agents:
        aid = a.get("agent_id")
        if aid is not None:
            user_agent_ids.add(str(aid))
            user_agent_ids.add(f"agent{aid}")

    results: list[DashboardScheduleInfo] = []
    for schedule in all_schedules:
        schedule_name = schedule.get("name", "")
        if schedule_name not in user_agent_ids:
            continue

        # Check that this schedule is linked to the requested dashboard
        # by inspecting the runtime variables (session_id) embedded in the trigger.
        variables = schedule.get("variables") or schedule.get("settings") or {}
        session_id = variables.get("session_id", "")
        if not session_id.startswith(dashboard_prefix):
            continue

        # Extract the numeric agent_id from the schedule name
        try:
            numeric_id = int(schedule_name.replace("agent", ""))
        except (ValueError, TypeError):
            continue

        results.append(
            DashboardScheduleInfo(
                schedule_id=str(schedule.get("id", "")),
                agent_id=numeric_id,
                interval=schedule.get("schedule_interval", ""),
                status=schedule.get("status", "unknown"),
                next_run=schedule.get("next_pipeline_run_at"),
                created_at=schedule.get("created_at"),
            )
        )

    return results


@router.delete(
    "/{dashboard_id}/schedules/{schedule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a dashboard refresh schedule",
)
async def delete_dashboard_schedule(
    dashboard_id: str,
    schedule_id: str,
    user: CurrentUser,
    svc: ServiceDep,
) -> Response:
    """Delete a Mage schedule trigger by ID."""

    # Verify dashboard exists
    try:
        await svc.get(dashboard_id)
    except DashboardNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Dashboard not found: {dashboard_id}",
        )

    # Verify ownership: the schedule must belong to one of the user's agents
    orchestrator = get_orchestrator()
    client = orchestrator.client

    try:
        all_schedules = await asyncio.to_thread(
            orchestrator.client.get_pipeline_schedules,
            orchestrator.PIPELINE_UUID,
        )
    except Exception:
        all_schedules = []

    user_agents = fetch_agents(user.email)
    user_agent_ids: set[str] = set()
    for a in user_agents:
        aid = a.get("agent_id")
        if aid is not None:
            user_agent_ids.add(str(aid))
            user_agent_ids.add(f"agent{aid}")

    target_schedule = None
    for s in all_schedules:
        if str(s.get("id")) == schedule_id:
            target_schedule = s
            break

    if target_schedule is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Schedule not found: {schedule_id}",
        )

    if target_schedule.get("name", "") not in user_agent_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not own this schedule.",
        )

    # Delete the schedule via Mage API
    url = (
        f"{client.base_url}/api/pipeline_schedules/{schedule_id}"
        f"?project={client.project_name}"
    )

    try:
        response = await asyncio.to_thread(
            requests.delete, url, headers=client._get_headers(), timeout=15
        )
        if response.status_code not in (200, 204):
            logger.error(
                f"[REFRESH] Mage returned {response.status_code} deleting schedule {schedule_id}: "
                f"{response.text}"
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Mage failed to delete schedule: {response.status_code}",
            )
    except requests.RequestException as exc:
        logger.error(
            f"[REFRESH] Failed to delete schedule {schedule_id}: {exc}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to contact Mage API: {exc}",
        )

    logger.info(
        f"[REFRESH] Deleted schedule {schedule_id} for dashboard {dashboard_id}"
    )

    return Response(status_code=status.HTTP_204_NO_CONTENT)

"""
Chart refresh scheduling — connects charts to agents via Mage cron triggers.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from apowerb.auth.dependencies import get_current_user
from apowerb.bi.charts.service import ChartNotFoundError, ChartService
from apowerb.bi.dependencies import get_chart_service
from apowerb.configs.th2logger import setup_logging
from apowerb.core.agent_main import get_agent_by_id
from apowerb.scheduler.run_agent_background import schedule_agent_run
from apowerb.users import schemas as user_schemas

logger = setup_logging(__name__)

router = APIRouter(prefix="/charts", tags=["chart-refresh"])

# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

ServiceDep = Annotated[ChartService, Depends(get_chart_service)]
CurrentUser = Annotated[user_schemas.User, Depends(get_current_user)]

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ScheduleChartRefreshRequest(BaseModel):
    agent_id: int
    interval: str = Field(
        ...,
        description="Cron expression or preset: @hourly, @daily, @weekly, @monthly",
        examples=["@daily", "0 8 * * *", "@hourly"],
    )
    start_time: datetime | None = None
    message_template: str | None = Field(
        default=None,
        description="Custom message sent to the agent. Default: 'Refresh chart {title}'",
    )


class ScheduleChartRefreshResponse(BaseModel):
    success: bool
    schedule_id: str | None = None
    chart_id: str
    agent_id: int
    interval: str
    message: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/{chart_id}/schedule-refresh",
    response_model=ScheduleChartRefreshResponse,
    summary="Schedule automatic refresh of a chart via an agent",
)
async def schedule_chart_refresh(
    chart_id: str,
    body: ScheduleChartRefreshRequest,
    user: CurrentUser,
    svc: ServiceDep,
) -> ScheduleChartRefreshResponse:
    """Create a cron-based schedule that triggers an agent to refresh a chart."""

    # 1. Verify chart exists
    try:
        chart = await svc.get(chart_id)
    except ChartNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Chart not found: {chart_id}",
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
            f"Refresh the chart '{chart.title}': "
            "query all data sources and update with the latest data."
        )

    new_message = {"role": "user", "content": message_text}

    # 4. Prepare start_time
    start_time_str: str | None = None
    if body.start_time:
        start_time_str = body.start_time.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    # 5. Call schedule_agent_run to create the Mage cron trigger
    mage_agent_id = agent_info.get("agent_id", f"agent{body.agent_id}")
    session_id = f"chart-refresh-{chart_id}-{uuid.uuid4().hex[:8]}"

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
            f"[REFRESH] Failed to schedule refresh for chart {chart_id}: {exc}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create schedule via Mage: {exc}",
        )

    logger.info(
        f"[REFRESH] Scheduled refresh for chart {chart_id} "
        f"with agent {body.agent_id}, interval={body.interval}"
    )

    return ScheduleChartRefreshResponse(
        success=True,
        schedule_id=str(result.get("schedule_id")),
        chart_id=chart_id,
        agent_id=body.agent_id,
        interval=body.interval,
        message=result.get("message", "Chart refresh scheduled successfully."),
    )

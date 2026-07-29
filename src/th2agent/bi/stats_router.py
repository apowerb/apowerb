"""
bi/stats_router.py
------------------
Lightweight BI statistics endpoint.

Returns aggregate counts for dashboards and charts so the frontend can
render summary cards without fetching every item.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from th2agent.auth.dependencies import get_current_user
from th2agent.users import schemas as user_schemas
from th2agent.bi.db_stores import DatabaseChartStore, DatabaseDashboardStore
from th2agent.helpers.database import get_db

router = APIRouter(tags=["bi-stats"])


@router.get(
    "/bi/stats",
    summary="BI module statistics",
    description="Returns the current count of dashboards and charts.",
)
async def bi_stats(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: user_schemas.User = Depends(get_current_user),
) -> dict:
    owner = str(current_user.email)
    chart_count = await DatabaseChartStore(db, owner=owner).count()
    dashboard_count = await DatabaseDashboardStore(db, owner=owner).count()
    return {
        "dashboard_count": dashboard_count,
        "chart_count": chart_count,
    }

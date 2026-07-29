"""Shared BI dependencies — DB-backed stores and service factories.

All BI routers (charts, dashboards, data) share the same store instances
backed by the ``business_intelligence`` DB table so data persists across
server restarts.  Stores are scoped to the current user (owner filter).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from th2agent.auth.dependencies import get_current_user
from th2agent.bi.charts.service import ChartService
from th2agent.bi.dashboards.service import DashboardService
from th2agent.bi.data.service import ChartDataService
from th2agent.bi.db_stores import DatabaseChartStore, DatabaseDashboardStore
from th2agent.helpers.database import get_db
from th2agent.users import schemas as user_schemas


# ---------------------------------------------------------------------------
# Service factories (used as FastAPI dependencies)
# ---------------------------------------------------------------------------


def get_chart_service(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[user_schemas.User, Depends(get_current_user)],
) -> ChartService:
    store = DatabaseChartStore(db, owner=user.email)
    return ChartService(store, db)


def get_dashboard_service(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[user_schemas.User, Depends(get_current_user)],
) -> DashboardService:
    store = DatabaseDashboardStore(db, owner=user.email)
    return DashboardService(store)


def get_data_service(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[user_schemas.User, Depends(get_current_user)],
) -> ChartDataService:
    chart_store = DatabaseChartStore(db, owner=user.email)
    chart_svc = ChartService(chart_store, db)
    return ChartDataService(chart_svc)

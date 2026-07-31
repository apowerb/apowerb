"""
data/router.py
--------------
FastAPI router — exposes GET /charts/{chart_id}/data.

Mount in main.py
----------------
    from data.router import router as data_router
    app.include_router(data_router, prefix="/api/v1")

Override the service dependency in tests
-----------------------------------------
    app.dependency_overrides[get_data_service] = lambda: ChartDataService(mock_store)
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from apowerb.bi.charts.core import FilterOperator, SortOrder
from apowerb.bi.data.schema import ChartDataResponse, DataRequest
from apowerb.bi.data.service import (
    ChartDataService,
    NoDataSourceError,
    QueryExecutionError,
)
from apowerb.bi.charts.service import ChartNotFoundError
from apowerb.bi.dependencies import get_data_service
from apowerb.auth.dependencies import get_current_user
from apowerb.helpers.database import get_db
from apowerb.users import schemas as user_schemas

router = APIRouter(tags=["chart-data"])

# ---------------------------------------------------------------------------
# Dependency wiring
# ---------------------------------------------------------------------------

DataServiceDep = Annotated[ChartDataService, Depends(get_data_service)]
CurrentUser = Annotated[user_schemas.User, Depends(get_current_user)]


# ---------------------------------------------------------------------------
# Public endpoint (no auth)
# ---------------------------------------------------------------------------


@router.get(
    "/public/charts/{chart_id}/data",
    response_model=ChartDataResponse,
    summary="Get chart data (public access)",
    responses={404: {"description": "Chart not found"}},
)
async def get_public_chart_data(
    chart_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    req: DataRequest = Depends(),
):
    from apowerb.bi.db_stores import DatabaseChartStore
    from apowerb.bi.charts.service import ChartService
    from apowerb.bi.data.service import (
        ChartDataService as _ChartDataService,
        NoDataSourceError as _NoDataSourceError,
        QueryExecutionError as _QueryExecutionError,
    )

    chart_store = DatabaseChartStore(db, owner=None)
    chart_svc = ChartService(chart_store, db)
    data_svc = _ChartDataService(chart_svc)
    try:
        chart = await chart_svc.get(chart_id)
        owner_id = chart.created_by
        return await data_svc.fetch(chart_id, req, user_id=owner_id, db_session=db)
    except ChartNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except _NoDataSourceError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except _QueryExecutionError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Chart data source failed (source={exc.source_type}). See server logs.",
        )


# ---------------------------------------------------------------------------
# Authenticated endpoint
# ---------------------------------------------------------------------------


@router.get(
    "/charts/{chart_id}/data",
    response_model=ChartDataResponse,
    summary="Fetch paginated data for a chart",
    description=(
        "Resolves the chart config by ID, executes its query, applies filters "
        "and sorting, then returns a paginated raw-data response. "
        "The frontend is responsible for mapping this to an ECharts option."
    ),
    responses={404: {"description": "Chart not found"}},
)
async def get_chart_data(
    chart_id: str,
    _user: CurrentUser,
    svc: DataServiceDep,
    db: Annotated[AsyncSession, Depends(get_db)],
    # pagination
    page: int = Query(default=1, ge=1, description="Page number (1-based)"),
    page_size: int | None = Query(
        default=None, ge=1, le=100_000,
        description=(
            "Rows per page. When unset, the service falls back to "
            "chart.source.limit (operator-configured per chart, "
            "default 1000). Capped at 100_000."
        ),
    ),
    # ad-hoc filter (layered on top of chart-level filters)
    filter_field: str | None = Query(
        default=None, description="Field name to filter on"
    ),
    filter_op: FilterOperator | None = Query(
        default=None, description="Filter operator"
    ),
    filter_value: str | None = Query(default=None, description="Filter value"),
    # ad-hoc sort (overrides chart.source.sort when provided)
    sort_field: str | None = Query(default=None, description="Field to sort by"),
    sort_order: SortOrder = Query(default=SortOrder.DESC, description="asc or desc"),
) -> ChartDataResponse:
    req = DataRequest(
        page=page,
        page_size=page_size,
        filter_field=filter_field,
        filter_op=filter_op,
        filter_value=filter_value,
        sort_field=sort_field,
        sort_order=sort_order,
    )

    try:
        return await svc.fetch(chart_id, req, user_id=_user.email, db_session=db)
    except ChartNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except NoDataSourceError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except QueryExecutionError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Chart data source failed (source={exc.source_type}). See server logs.",
        )

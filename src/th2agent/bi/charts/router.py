"""
router.py
---------
FastAPI router for the /charts endpoints.

Mount in your main app
-----------------------
    from router import router as charts_router
    app.include_router(charts_router, prefix="/api/v1")

Dependency injection
--------------------
Override `get_service` in tests or when wiring a real DB store:

    app.dependency_overrides[get_service] = lambda: ChartService(MyDBStore())
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse

from th2agent.bi.charts.schemas import (
    ChartCreateRequest,
    ChartListResponse,
    ChartResponse,
    ChartUpdateRequest,
    ErrorResponse,
)
from th2agent.bi.charts.service import (
    ChartNotFoundError,
    ChartPermissionError,
    ChartConflictError,
    ChartService,
)
from th2agent.bi.data.service import ChartDataService
from th2agent.bi.dependencies import get_chart_service
from th2agent.auth.dependencies import get_current_user
from th2agent.users import schemas as user_schemas

router = APIRouter(prefix="/charts", tags=["charts"])

# ---------------------------------------------------------------------------
# Dependency
# ---------------------------------------------------------------------------

ServiceDep = Annotated[ChartService, Depends(get_chart_service)]
CurrentUser = Annotated[user_schemas.User, Depends(get_current_user)]

def _load_charts_on_startup() -> None:
    """No-op — charts are now persisted in the DB via DatabaseChartStore."""
    pass


# ---------------------------------------------------------------------------
# Exception handlers (register on the app, not the router)
# ---------------------------------------------------------------------------


def register_exception_handlers(app) -> None:  # noqa: ANN001
    @app.exception_handler(ChartConflictError)
    async def conflict_handler(request, exc: ChartConflictError):  # noqa: ANN001
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=ErrorResponse(detail=str(exc), code="CHART_CONFLICT").model_dump(),
        )

    @app.exception_handler(ChartPermissionError)
    async def permission_handler(request, exc: ChartPermissionError):  # noqa: ANN001
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content=ErrorResponse(detail=str(exc), code="CHART_FORBIDDEN").model_dump(),
        )
    @app.exception_handler(ChartNotFoundError)
    async def not_found_handler(request, exc: ChartNotFoundError):  # noqa: ANN001
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content=ErrorResponse(detail=str(exc), code="CHART_NOT_FOUND").model_dump(),
        )



# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=ChartListResponse,
    summary="List charts",
    description="Returns a paginated list of charts. Filter by viewer role to enforce visibility.",
)
async def list_charts(
    _user: CurrentUser,
    svc: ServiceDep,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    role: str | None = Query(
        default=None, description="Viewer role for permission filtering"
    ),
) -> ChartListResponse:
    charts, total = await svc.list(page=page, page_size=page_size, viewer_role=role)
    return ChartListResponse.paginate(
        charts, page=page, page_size=page_size, total=total
    )


@router.post(
    "",
    response_model=ChartResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a chart",
)
async def create_chart(
    body: ChartCreateRequest,
    user: CurrentUser,
    svc: ServiceDep,
) -> ChartResponse:
    try:
        chart = await svc.create(body, created_by=user.email)
    except ChartConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return ChartResponse.from_domain(chart)


@router.get(
    "/{chart_id}",
    response_model=ChartResponse,
    summary="Get a single chart",
    responses={404: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)
async def get_chart(
    chart_id: str,
    _user: CurrentUser,
    svc: ServiceDep,
    role: str | None = Query(default=None),
) -> ChartResponse:
    try:
        chart = await svc.get(chart_id, viewer_role=role)
    except ChartNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ChartPermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    return ChartResponse.from_domain(chart)


@router.patch(
    "/{chart_id}",
    response_model=ChartResponse,
    summary="Partially update a chart",
    responses={404: {"model": ErrorResponse}},
)
async def update_chart(
    chart_id: str,
    body: ChartUpdateRequest,
    _user: CurrentUser,
    svc: ServiceDep,
) -> ChartResponse:
    try:
        chart = await svc.update(chart_id, body)
    except ChartNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    ChartDataService.invalidate_cache(chart_id)
    return ChartResponse.from_domain(chart)


@router.delete(
    "/{chart_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a chart",
    responses={404: {"model": ErrorResponse}},
)
async def delete_chart(chart_id: str, _user: CurrentUser, svc: ServiceDep) -> None:
    try:
        await svc.delete(chart_id)
    except ChartNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    ChartDataService.invalidate_cache(chart_id)


# ---------------------------------------------------------------------------
# Shortcut endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/shortcuts/kpi",
    response_model=ChartResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a KPI stat card",
    tags=["charts", "shortcuts"],
)
async def create_kpi(
    _user: CurrentUser,
    svc: ServiceDep,
    name  : str = Query(...),
    title: str = Query(...),
    query: str = Query(...),
    organization_id: str = Query(...),
    project_id: str = Query(default="thaink2"),
    refresh_interval: int = Query(default=30, gt=0),
    created_by: str | None = Query(default=None),
) -> ChartResponse:
    chart = await svc.create_kpi(
        name=name,
        title=title,
        query=query,
        organization_id=organization_id,
        project_id=project_id,
        refresh_interval=refresh_interval,
        created_by=created_by,
    )
    return ChartResponse.from_domain(chart)


@router.post(
    "/shortcuts/timeseries",
    response_model=ChartResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a time-series chart",
    tags=["charts", "shortcuts"],
)
async def create_timeseries(
    _user: CurrentUser,
    svc: ServiceDep,
    title: str = Query(...),
    name  : str = Query(...),
    query: str = Query(...),
    time_field: str = Query(default="ts"),
    organization_id: str = Query(...),
    project_id: str = Query(default="thaink2"),
    refresh_interval: int = Query(default=60, gt=0),
    created_by: str | None = Query(default=None),
) -> ChartResponse:
    chart = await svc.create_timeseries(
        title=title,
        name=name,
        query=query,
        time_field=time_field,
        refresh_interval=refresh_interval,
        organization_id=organization_id,
        project_id=project_id,
        created_by=created_by,
    )
    return ChartResponse.from_domain(chart)


@router.post(
    "/{chart_id}/send-to-dashboard",
    summary="Add a chart to the user's chat dashboard (user-triggered)",
    tags=["charts"],
)
async def send_chart_to_dashboard(
    chart_id: str,
    user: CurrentUser,
    session_id: str | None = Query(default=None),
) -> dict:
    """Add ``chart_id`` to the current chat's dashboard (created on first use).

    Backs the "Send to dashboard" button on an inline chart card: charts are no
    longer added automatically, so the user triggers this explicitly. The
    dashboard is scoped to ``session_id`` (the conversation) when provided.
    """
    from th2agent.tools_store.portfolio.business_intelligence import (
        _async_ensure_chat_dashboard,
    )

    res = await _async_ensure_chat_dashboard(
        chart_id, user.email, session_id=session_id
    )
    if not res.get("success"):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=res.get("error") or "Could not add the chart to the dashboard.",
        )
    return {"dashboard_id": res["dashboard_id"]}

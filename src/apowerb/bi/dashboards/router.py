"""
dashboard/router.py
-------------------
FastAPI router — all /dashboards endpoints.

Mount in main.py
----------------
    from dashboard.router import router as dashboard_router
    app.include_router(dashboard_router, prefix="/api/v1")

Override the service in tests
------------------------------
    app.dependency_overrides[get_dashboard_service] = lambda: DashboardService(mock_store)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from apowerb.bi.dashboards.core import DashboardStatus, DashboardVisibility
from apowerb.bi.dashboards.schema import (
    AddComponentRequest,
    UpdateComponentRequest,
    DashboardCreateRequest,
    DashboardListResponse,
    DashboardResponse,
    DashboardUpdateRequest,
    ErrorResponse,
    MoveComponentRequest,
    PublishRequest,
)
from apowerb.bi.dashboards.service import (
    ComponentNotFoundError,
    DashboardNotFoundError,
    DashboardPermissionError,
    DashboardService,
    SlugConflictError,
)
from apowerb.bi.dependencies import get_dashboard_service
from apowerb.auth.dependencies import get_current_user, get_optional_user
from apowerb.helpers.database import get_db
from apowerb.users import schemas as user_schemas

router = APIRouter(prefix="/dashboards", tags=["dashboards"])

# ---------------------------------------------------------------------------
# Dependency
# ---------------------------------------------------------------------------

ServiceDep = Annotated[DashboardService, Depends(get_dashboard_service)]
CurrentUser = Annotated[user_schemas.User, Depends(get_current_user)]


# ---------------------------------------------------------------------------
# Dashboard CRUD
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=DashboardListResponse,
    summary="List dashboards",
)
async def list_dashboards(
    _user: CurrentUser,
    svc: ServiceDep,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    status: DashboardStatus | None = Query(default=None),
    role: str | None = Query(
        default=None, description="Viewer role for permission filtering"
    ),
) -> DashboardListResponse:
    dashboards, total = await svc.list(
        page=page, page_size=page_size, viewer_role=role, status=status
    )
    return DashboardListResponse.paginate(
        dashboards, page=page, page_size=page_size, total=total
    )


@router.get(
    "/shared",
    response_model=DashboardListResponse,
    summary="List published dashboards shared with the current user",
)
async def list_shared_dashboards(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> DashboardListResponse:
    from apowerb.bi.db_stores import DatabaseDashboardStore
    from apowerb.helpers.emails import (
        get_domain_from_email,
        get_domain_from_email_or_none,
    )

    # Query all published dashboards (no owner filter)
    store = DatabaseDashboardStore(db, owner=None)
    all_dashboards, _ = await store.list(page=1, page_size=200)

    viewer_domain = get_domain_from_email(user.email)
    shared = []
    for d in all_dashboards:
        # Skip own dashboards
        if d.created_by == user.email:
            continue
        # Must be published
        if d.status != DashboardStatus.PUBLISHED:
            continue
        vis = d.visibility if d.visibility != DashboardVisibility.PRIVATE else DashboardVisibility.PUBLIC
        if vis == DashboardVisibility.PUBLIC:
            shared.append(d)
        elif vis == DashboardVisibility.ORGANIZATION:
            owner_domain = get_domain_from_email_or_none(d.created_by)
            if owner_domain is not None and viewer_domain == owner_domain:
                shared.append(d)

    total = len(shared)
    start = (page - 1) * page_size
    page_items = shared[start : start + page_size]
    return DashboardListResponse.paginate(
        page_items, page=page, page_size=page_size, total=total
    )


@router.post(
    "",
    response_model=DashboardResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a dashboard",
    responses={409: {"model": ErrorResponse, "description": "Slug already in use"}},
)
async def create_dashboard(
    body: DashboardCreateRequest,
    user: CurrentUser,
    svc: ServiceDep,
) -> DashboardResponse:
    try:
        dashboard = await svc.create(body, created_by=user.email)
    except SlugConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return DashboardResponse.from_domain(dashboard)


@router.get(
    "/{dashboard_id}",
    response_model=DashboardResponse,
    summary="Get a dashboard by ID",
    responses={404: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)
async def get_dashboard(
    dashboard_id: str,
    _user: CurrentUser,
    svc: ServiceDep,
    role: str | None = Query(default=None),
) -> DashboardResponse:
    try:
        dashboard = await svc.get(dashboard_id, viewer_role=role)
    except DashboardNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except DashboardPermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    return DashboardResponse.from_domain(dashboard)


@router.get(
    "/by-slug/{slug}",
    response_model=DashboardResponse,
    summary="Get a dashboard by slug",
    responses={404: {"model": ErrorResponse}},
)
async def get_dashboard_by_slug(
    slug: str,
    _user: CurrentUser,
    svc: ServiceDep,
) -> DashboardResponse:
    try:
        dashboard = await svc.get_by_slug(slug)
    except DashboardNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return DashboardResponse.from_domain(dashboard)


@router.get(
    "/public/{slug}",
    response_model=DashboardResponse,
    summary="Get published dashboard by slug (public access)",
)
async def get_public_dashboard(
    slug: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[user_schemas.User | None, Depends(get_optional_user)],
):
    from apowerb.bi.db_stores import DatabaseDashboardStore
    from apowerb.helpers.emails import (
        get_domain_from_email,
        get_domain_from_email_or_none,
    )

    store = DatabaseDashboardStore(db, owner=None)  # no owner filter
    dashboard = await store.get_by_slug(slug)

    if not dashboard or dashboard.status != DashboardStatus.PUBLISHED:
        raise HTTPException(status_code=404, detail="Dashboard not found")

    # Both public and organization require authentication
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")

    # Published dashboards default to public visibility (backwards compat)
    vis = dashboard.visibility if dashboard.visibility != DashboardVisibility.PRIVATE else DashboardVisibility.PUBLIC

    if vis == DashboardVisibility.PUBLIC:
        # Any logged-in user can view
        return DashboardResponse.from_domain(dashboard)

    if vis == DashboardVisibility.ORGANIZATION:
        # Only users from the same email domain
        viewer_domain = get_domain_from_email(user.email)
        owner_domain = get_domain_from_email_or_none(dashboard.created_by)
        if owner_domain is None or viewer_domain != owner_domain:
            raise HTTPException(status_code=403, detail="Not in the same organization")
        return DashboardResponse.from_domain(dashboard)

    raise HTTPException(status_code=403, detail="Dashboard is private")


@router.patch(
    "/{dashboard_id}",
    response_model=DashboardResponse,
    summary="Partially update a dashboard",
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
async def update_dashboard(
    dashboard_id: str,
    body: DashboardUpdateRequest,
    _user: CurrentUser,
    svc: ServiceDep,
) -> DashboardResponse:
    try:
        dashboard = await svc.update(dashboard_id, body)
    except DashboardNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except SlugConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return DashboardResponse.from_domain(dashboard)


@router.post(
    "/{dashboard_id}/publish",
    response_model=DashboardResponse,
    summary="Publish a dashboard",
    responses={404: {"model": ErrorResponse}},
)
async def publish_dashboard(
    dashboard_id: str,
    body: PublishRequest,
    _user: CurrentUser,
    svc: ServiceDep,
) -> DashboardResponse:
    try:
        dashboard = await svc.publish(dashboard_id, visibility=body.visibility)
    except DashboardNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return DashboardResponse.from_domain(dashboard)


@router.post(
    "/{dashboard_id}/unpublish",
    response_model=DashboardResponse,
    summary="Unpublish a dashboard",
    responses={404: {"model": ErrorResponse}},
)
async def unpublish_dashboard(
    dashboard_id: str,
    _user: CurrentUser,
    svc: ServiceDep,
) -> DashboardResponse:
    try:
        dashboard = await svc.unpublish(dashboard_id)
    except DashboardNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return DashboardResponse.from_domain(dashboard)


@router.delete(
    "/{dashboard_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a dashboard",
    responses={404: {"model": ErrorResponse}},
)
async def delete_dashboard(dashboard_id: str, _user: CurrentUser, svc: ServiceDep) -> None:
    try:
        await svc.delete(dashboard_id)
    except DashboardNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


# ---------------------------------------------------------------------------
# Component management
# ---------------------------------------------------------------------------


@router.post(
    "/{dashboard_id}/components",
    response_model=DashboardResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a component to a dashboard",
    responses={404: {"model": ErrorResponse}},
)
async def add_component(
    dashboard_id: str,
    body: AddComponentRequest,
    _user: CurrentUser,
    svc: ServiceDep,
) -> DashboardResponse:
    try:
        dashboard = await svc.add_component(dashboard_id, body)
    except DashboardNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return DashboardResponse.from_domain(dashboard)


@router.delete(
    "/{dashboard_id}/components/{component_id}",
    response_model=DashboardResponse,
    summary="Remove a component from a dashboard",
    responses={404: {"model": ErrorResponse}},
)
async def remove_component(
    dashboard_id: str,
    component_id: str,
    _user: CurrentUser,
    svc: ServiceDep,
) -> DashboardResponse:
    try:
        dashboard = await svc.remove_component(dashboard_id, component_id)
    except (DashboardNotFoundError, ComponentNotFoundError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return DashboardResponse.from_domain(dashboard)


@router.patch(
    "/{dashboard_id}/components/{component_id}/position",
    response_model=DashboardResponse,
    summary="Move a component to a new grid position",
    responses={404: {"model": ErrorResponse}},
)
async def move_component(
    dashboard_id: str,
    component_id: str,
    body: MoveComponentRequest,
    _user: CurrentUser,
    svc: ServiceDep,
) -> DashboardResponse:
    try:
        dashboard = await svc.move_component(dashboard_id, component_id, body)
    except (DashboardNotFoundError, ComponentNotFoundError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return DashboardResponse.from_domain(dashboard)


@router.patch(
    "/{dashboard_id}/components/{component_id}",
    response_model=DashboardResponse,
    summary="Update a key-value component's content",
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
async def update_component(
    dashboard_id: str,
    component_id: str,
    body: UpdateComponentRequest,
    _user: CurrentUser,
    svc: ServiceDep,
) -> DashboardResponse:
    try:
        dashboard = await svc.update_component(dashboard_id, component_id, body)
    except (DashboardNotFoundError, ComponentNotFoundError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return DashboardResponse.from_domain(dashboard)


# ---------------------------------------------------------------------------
# Agent linkage
# ---------------------------------------------------------------------------


class LinkAgentRequest(BaseModel):
    agent_id: int | None = Field(
        ..., description="Agent ID to link. Set to null to unlink."
    )


@router.patch(
    "/{dashboard_id}/agent",
    response_model=DashboardResponse,
    summary="Link or unlink an agent to a dashboard",
    responses={404: {"model": ErrorResponse}},
)
async def link_agent_to_dashboard(
    dashboard_id: str,
    body: LinkAgentRequest,
    user: CurrentUser,
    svc: ServiceDep,
) -> DashboardResponse:
    """Associate an agent with a dashboard for traceability and refresh scheduling."""
    try:
        dashboard = await svc.get(dashboard_id)
    except DashboardNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))

    # Verify agent exists if linking
    if body.agent_id is not None:
        from apowerb.core.agent_main import get_agent_by_id

        agent = get_agent_by_id(str(body.agent_id), user.email)
        if not agent:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Agent not found: {body.agent_id}",
            )

    updated = dashboard.model_copy(
        update={
            "agent_id": body.agent_id,
            "updated_at": datetime.now(timezone.utc),
            "version": dashboard.version + 1,
        }
    )
    saved = await svc._store.save(updated)
    return DashboardResponse.from_domain(saved)


@router.get(
    "/{dashboard_id}/agent",
    summary="Get agent linked to dashboard",
    responses={404: {"model": ErrorResponse}},
)
async def get_dashboard_agent(
    dashboard_id: str,
    user: CurrentUser,
    svc: ServiceDep,
) -> dict:
    """Get the agent associated with a dashboard."""
    try:
        dashboard = await svc.get(dashboard_id)
    except DashboardNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))

    if dashboard.agent_id is None:
        return {"agent_id": None, "agent_name": None}

    from apowerb.core.agent_main import get_agent_by_id

    agent = get_agent_by_id(str(dashboard.agent_id), user.email)
    if agent:
        return {
            "agent_id": dashboard.agent_id,
            "agent_name": agent.get("agent_name", f"agent{dashboard.agent_id}"),
        }
    return {"agent_id": dashboard.agent_id, "agent_name": f"agent{dashboard.agent_id}"}

"""
bi/spec/router.py
-----------------
Spec v2 endpoints:

POST /dashboards/{id}/data                 computed data of the visuals
POST /public/dashboards/{slug}/data        same, for a published dashboard
POST /dashboards/{id}/spec                 apply patch operations (or dry-run)
GET  /dashboard-templates                  templates a dashboard can be created from
POST /dashboard-templates/{id}/dashboards  create a draft dashboard from a template

Access rules are those of the existing dashboard routes: the owner-scoped
service for ``/dashboards/{id}*`` (same as ``GET`` / ``PATCH /dashboards/{id}``),
and the published-dashboard rule of ``GET /dashboards/public/{slug}`` for the
public route. The data always reads with the dashboard owner's access, never
the viewer's.

Mount in main.py:  app.include_router(spec_router, prefix="/api/v1")
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from apowerb.auth.dependencies import get_current_user, get_optional_user
from apowerb.bi.dashboards.access import visible_to
from apowerb.bi.dashboards.core import Dashboard, DashboardStatus
from apowerb.bi.dashboards.core import ComponentType
from apowerb.bi.dashboards.schema import (
    ComponentCreateSchema,
    DashboardCreateRequest,
    DashboardResponse,
    ErrorResponse,
    GridPositionSchema,
)
from apowerb.bi.dashboards.service import (
    DashboardNameConflictError,
    DashboardNotFoundError,
    DashboardPermissionError,
    DashboardService,
    create_with_unique_title,
)
from apowerb.bi.dependencies import get_dashboard_service
from apowerb.bi.spec import catalog, sources
from apowerb.bi.spec.engine import compute_dashboard
from apowerb.bi.spec.errors import SpecError
from apowerb.bi.spec.models import (
    DashboardDataRequest,
    DashboardDataResponse,
    FilterSpec,
    PatchRequest,
    SourceSpec,
    VisualSpec,
)
from apowerb.bi.spec.ops import apply_ops
from apowerb.helpers.database import get_db
from apowerb.users import schemas as user_schemas

logger = logging.getLogger(__name__)

router = APIRouter(tags=["dashboard-spec"])

ServiceDep = Annotated[DashboardService, Depends(get_dashboard_service)]
CurrentUser = Annotated[user_schemas.User, Depends(get_current_user)]


def _owner_of(dashboard: Dashboard, fallback: str) -> str:
    """Data owner: the dashboard's owner (row ``owner`` column), never the viewer."""
    return dashboard.created_by or fallback


def _unprocessable(exc: SpecError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.errors)


async def _compute(dashboard: Dashboard, body: DashboardDataRequest, owner: str) -> DashboardDataResponse:
    try:
        return await compute_dashboard(dashboard, body, owner=owner)
    except SpecError as exc:
        raise _unprocessable(exc)
    except Exception:  # noqa: BLE001 - never leak a trace to the client
        logger.exception("[BiSpec] dashboard %s computation failed", dashboard.id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Le calcul du tableau de bord a échoué (voir les journaux du serveur).",
        )


@router.post(
    "/dashboards/{dashboard_id}/data",
    response_model=DashboardDataResponse,
    summary="Compute the data of a dashboard's visuals",
    responses={404: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 422: {"description": "Invalid filters"}},
)
async def get_dashboard_data(
    dashboard_id: str,
    body: DashboardDataRequest,
    user: CurrentUser,
    svc: ServiceDep,
    role: str | None = Query(default=None),
) -> DashboardDataResponse:
    try:
        dashboard = await svc.get(dashboard_id, viewer_role=role)
    except DashboardNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except DashboardPermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    return await _compute(dashboard, body, _owner_of(dashboard, user.email))


@router.post(
    "/public/dashboards/{slug}/data",
    response_model=DashboardDataResponse,
    summary="Compute the data of a published dashboard",
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
async def get_public_dashboard_data(
    slug: str,
    body: DashboardDataRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[user_schemas.User | None, Depends(get_optional_user)],
) -> DashboardDataResponse:
    from apowerb.bi.db_stores import DatabaseDashboardStore

    store = DatabaseDashboardStore(db, owner=None)  # no owner filter
    dashboard = await store.get_by_slug(slug)
    # An unpublished dashboard is a 404, whoever asks (same as GET /dashboards/public/{slug}).
    if not dashboard or dashboard.status != DashboardStatus.PUBLISHED:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dashboard not found")
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    if not visible_to(dashboard, user.email):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not in the same organization")
    if not dashboard.created_by:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dashboard not found")
    return await _compute(dashboard, body, dashboard.created_by)


@router.post(
    "/dashboards/{dashboard_id}/spec",
    summary="Apply patch operations to a dashboard's spec",
    responses={404: {"model": ErrorResponse}, 422: {"description": "Invalid spec: list of messages"}},
)
async def patch_dashboard_spec(
    dashboard_id: str,
    body: PatchRequest,
    user: CurrentUser,
    svc: ServiceDep,
) -> Any:
    try:
        dashboard = await svc.get(dashboard_id)
    except DashboardNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    try:
        updated = apply_ops(dashboard, body.ops)
    except SpecError as exc:
        raise _unprocessable(exc)
    if body.dry_run:
        data = await _compute(updated, DashboardDataRequest(), _owner_of(updated, user.email))
        return {"dashboard": DashboardResponse.from_domain(updated), "data": data}
    saved = await svc.save(updated)
    return DashboardResponse.from_domain(saved)


# ---------------------------------------------------------------------------
# Dashboard templates
# ---------------------------------------------------------------------------

_DATASET_NOT_FOUND = "Jeu de données introuvable"
_MAX_LISTED_COLUMNS = 30


class TemplateDashboardRequest(BaseModel):
    dataset_id: str = Field(..., min_length=1)
    title: str | None = Field(default=None, max_length=120)
    columns: dict[str, str | None] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)


class TemplateDashboardResponse(BaseModel):
    dashboard_id: str
    title: str
    url: str


def _invalid(errors: list[str]) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"errors": errors})


def _create_request(title: str, spec: dict) -> DashboardCreateRequest:
    return DashboardCreateRequest(
        title=title,
        sources=[SourceSpec.model_validate(s) for s in spec.get("sources") or []],
        filters=[FilterSpec.model_validate(f) for f in spec.get("filters") or []],
        components=[
            ComponentCreateSchema(
                component_type=ComponentType.VISUAL,
                position=GridPositionSchema(**(item.get("position") or {})),
                visual=VisualSpec.model_validate(item["visual"]),
            )
            for item in spec.get("components") or []
        ],
    )


def _missing_columns(template: catalog.DashboardTemplate, columns: dict[str, str | None], frame_columns) -> list[str]:
    available = [str(c) for c in frame_columns]
    errors = []
    for col in template.columns:
        name = columns.get(col.key)
        if name and name not in available:
            errors.append(
                f"La colonne « {name} » ({col.label}) est introuvable dans le jeu de données. "
                f"Colonnes disponibles : {', '.join(available[:_MAX_LISTED_COLUMNS])}."
            )
    return errors


@router.get("/dashboard-templates", summary="List the dashboard templates")
async def list_dashboard_templates(user: CurrentUser) -> list[dict[str, Any]]:
    return [t.describe() for t in catalog.list_templates()]


@router.post(
    "/dashboard-templates/{template_id}/dashboards",
    response_model=TemplateDashboardResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a draft dashboard from a template",
    responses={404: {"model": ErrorResponse}, 422: {"description": "Invalid columns or parameters: list of messages"}},
)
async def create_dashboard_from_template(
    template_id: str,
    body: TemplateDashboardRequest,
    user: CurrentUser,
    svc: ServiceDep,
) -> TemplateDashboardResponse:
    template = catalog.get_template(template_id)
    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Gabarit introuvable : {template_id}")

    columns, column_errors = template.clean_columns(body.columns)
    params, param_errors = template.clean_params(body.params)
    if column_errors or param_errors:
        raise _invalid(column_errors + param_errors)
    try:
        spec = template.build(body.dataset_id.strip(), columns, params)
    except ValueError as exc:
        raise _invalid([str(exc)])

    try:
        frame = await sources.load_dataset(body.dataset_id.strip(), user.email)
    except SpecError as exc:
        if str(exc).startswith(_DATASET_NOT_FOUND):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
        raise _invalid(exc.errors)
    missing = _missing_columns(template, columns, frame.columns)
    if missing:
        raise _invalid(missing)

    title = (body.title or "").strip() or template.label
    try:
        request = _create_request(title, spec)
        dashboard, final_title = await create_with_unique_title(
            title,
            lambda candidate: svc.create(request.model_copy(update={"title": candidate}), created_by=user.email),
        )
    except ValidationError as exc:
        raise _invalid([f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:8]])
    except SpecError as exc:
        raise _invalid(exc.errors)
    except DashboardNameConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return TemplateDashboardResponse(
        dashboard_id=dashboard.id, title=final_title, url=f"/bi/{dashboard.id}"
    )

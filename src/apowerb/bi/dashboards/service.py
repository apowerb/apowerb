"""
dashboard/service.py
--------------------
Business logic layer: DashboardService + persistence protocol.

Swap InMemoryDashboardStore for any async ORM/DB adapter by implementing
the DashboardStore protocol — nothing else in the stack changes.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Protocol, TypeVar

from sqlalchemy.exc import IntegrityError

from apowerb.bi.dashboards.core import (
    Dashboard,
    DashboardStatus,
    DashboardVisibility,
)
from apowerb.bi.dashboards.schema import (
    AddComponentRequest,
    UpdateComponentRequest,
    DashboardCreateRequest,
    DashboardUpdateRequest,
    MoveComponentRequest,
)


# ---------------------------------------------------------------------------
# Store protocol
# ---------------------------------------------------------------------------


class DashboardStore(Protocol):
    async def get(self, dashboard_id: str) -> Dashboard | None: ...
    async def get_by_slug(self, slug: str) -> Dashboard | None: ...
    async def list(
        self, *, page: int, page_size: int
    ) -> tuple[list[Dashboard], int]: ...
    async def save(self, dashboard: Dashboard) -> Dashboard: ...
    async def delete(self, dashboard_id: str) -> bool: ...


# ---------------------------------------------------------------------------
# In-memory store
# ---------------------------------------------------------------------------


class InMemoryDashboardStore:
    def __init__(self) -> None:
        self._data: dict[str, Dashboard] = {}

    async def get(self, dashboard_id: str) -> Dashboard | None:
        return self._data.get(dashboard_id)

    async def get_by_slug(self, slug: str) -> Dashboard | None:
        return next((d for d in self._data.values() if d.slug == slug), None)

    async def list(self, *, page: int, page_size: int) -> tuple[list[Dashboard], int]:
        all_items = sorted(
            self._data.values(), key=lambda d: d.created_at, reverse=True
        )
        total = len(all_items)
        start = (page - 1) * page_size
        return all_items[start : start + page_size], total

    async def save(self, dashboard: Dashboard) -> Dashboard:
        self._data[dashboard.id] = dashboard
        return dashboard

    async def delete(self, dashboard_id: str) -> bool:
        if dashboard_id in self._data:
            del self._data[dashboard_id]
            return True
        return False


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class DashboardNotFoundError(Exception):
    def __init__(self, identifier: str) -> None:
        self.identifier = identifier
        super().__init__(f"Dashboard '{identifier}' not found")


class DashboardPermissionError(Exception):
    def __init__(self, dashboard_id: str, role: str) -> None:
        super().__init__(f"Role '{role}' cannot access dashboard '{dashboard_id}'")


class ComponentNotFoundError(Exception):
    def __init__(self, dashboard_id: str, component_id: str) -> None:
        super().__init__(
            f"Component '{component_id}' not found in dashboard '{dashboard_id}'"
        )


class SlugConflictError(Exception):
    def __init__(self, slug: str) -> None:
        super().__init__(f"Slug '{slug}' is already in use")


class DashboardNameConflictError(Exception):
    def __init__(self, title: str) -> None:
        super().__init__(f"A dashboard named '{title}' already exists in this organization.")


T = TypeVar("T")

MAX_TITLE_ATTEMPTS = 20
_MAX_TITLE_LENGTH = 120


def _is_name_conflict(exc: Exception) -> bool:
    return isinstance(exc, DashboardNameConflictError)


def numbered_title(title: str, attempt: int) -> str:
    """``title`` for attempt 1, then ``title (2)``, ``title (3)``... within the title limit."""
    if attempt <= 1:
        return title
    suffix = f" ({attempt})"
    return title[: _MAX_TITLE_LENGTH - len(suffix)].rstrip() + suffix


async def create_with_unique_title(
    title: str,
    create: Callable[[str], Awaitable[T]],
    *,
    is_duplicate: Callable[[Exception], bool] = _is_name_conflict,
) -> tuple[T, str]:
    """Run ``create(candidate)`` with ``title``, then ``title (2)``... while the title is taken.

    Returns the created value and the title it was created under. Any other error
    propagates; after ``MAX_TITLE_ATTEMPTS`` names, raises ``DashboardNameConflictError``.
    """
    for attempt in range(1, MAX_TITLE_ATTEMPTS + 1):
        candidate = numbered_title(title, attempt)
        try:
            return await create(candidate), candidate
        except Exception as exc:
            if not is_duplicate(exc):
                raise
    raise DashboardNameConflictError(title)


def _check_spec(dashboard: Dashboard) -> None:
    """Refuse a spec v2 dashboard that references unknown sources, measures…"""
    if dashboard.spec_version < 2:
        return
    from apowerb.bi.spec.errors import SpecError
    from apowerb.bi.spec.validate import validate_spec

    errors = validate_spec(dashboard)
    if errors:
        raise SpecError("; ".join(errors), errors=errors)


# ---------------------------------------------------------------------------
# DashboardService
# ---------------------------------------------------------------------------


class DashboardService:
    """
    Orchestrates dashboard CRUD and component management.

    Example (FastAPI DI)
    --------------------
    store = InMemoryDashboardStore()
    svc   = DashboardService(store)

    @app.get("/dashboards/{id}")
    async def get(id: str, svc = Depends(get_dashboard_service)):
        return await svc.get(id)
    """

    def __init__(self, store: DashboardStore) -> None:
        self._store = store

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def get(
        self, dashboard_id: str, *, viewer_role: str | None = None
    ) -> Dashboard:
        dashboard = await self._store.get(dashboard_id)
        if dashboard is None:
            raise DashboardNotFoundError(dashboard_id)
        if viewer_role and not dashboard.is_visible_to(viewer_role):
            raise DashboardPermissionError(dashboard_id, viewer_role)
        return dashboard

    async def get_by_slug(self, slug: str) -> Dashboard:
        dashboard = await self._store.get_by_slug(slug)
        if dashboard is None:
            raise DashboardNotFoundError(slug)
        return dashboard

    async def list(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        viewer_role: str | None = None,
        status: DashboardStatus | None = None,
    ) -> tuple[list[Dashboard], int]:
        dashboards, total = await self._store.list(page=page, page_size=page_size)

        if viewer_role:
            dashboards = [d for d in dashboards if d.is_visible_to(viewer_role)]
        if status:
            dashboards = [d for d in dashboards if d.status == status]

        return dashboards, len(dashboards) if (viewer_role or status) else total

    # ------------------------------------------------------------------
    # Dashboard CRUD
    # ------------------------------------------------------------------

    async def create(
        self,
        req: DashboardCreateRequest,
        *,
        created_by: str | None = None,
    ) -> Dashboard:
        if req.slug:
            existing = await self._store.get_by_slug(req.slug)
            if existing:
                raise SlugConflictError(req.slug)

        dashboard = Dashboard.create(
            title=req.title,
            description=req.description,
            slug=req.slug,
            components=[c.to_domain() for c in req.components],
            columns=req.columns,
            permissions=req.permissions,
            created_by=created_by,
            agent_id=req.agent_id,
            sources=req.sources,
            filters=req.filters,
        )
        _check_spec(dashboard)
        return await self._save_named(dashboard)

    async def _save_named(self, dashboard: Dashboard) -> Dashboard:
        """Save; the (name, type, organization, project) unique key becomes a conflict."""
        try:
            return await self._store.save(dashboard)
        except IntegrityError as exc:
            db = getattr(self._store, "_db", None)
            if db is not None:
                await db.rollback()
            raise DashboardNameConflictError(dashboard.title) from exc

    async def update(self, dashboard_id: str, req: DashboardUpdateRequest) -> Dashboard:
        dashboard = await self._store.get(dashboard_id)
        if dashboard is None:
            raise DashboardNotFoundError(dashboard_id)

        if req.slug and req.slug != dashboard.slug:
            existing = await self._store.get_by_slug(req.slug)
            if existing:
                raise SlugConflictError(req.slug)

        updates: dict = {"updated_at": datetime.now(timezone.utc)}
        for field in (
            "title",
            "description",
            "slug",
            "columns",
            "status",
            "permissions",
            "agent_id",
        ):
            val = getattr(req, field)
            if val is not None:
                updates[field] = val

        updated = dashboard.model_copy(update=updates)
        return await self._save_named(updated)

    async def publish(self, dashboard_id: str, visibility: DashboardVisibility = DashboardVisibility.PRIVATE) -> Dashboard:
        dashboard = await self.get(dashboard_id)
        published = dashboard.publish(visibility=visibility)
        return await self._store.save(published)

    async def unpublish(self, dashboard_id: str) -> Dashboard:
        dashboard = await self.get(dashboard_id)
        unpublished = dashboard.unpublish()
        return await self._store.save(unpublished)

    async def save(self, dashboard: Dashboard) -> Dashboard:
        """Persist an already-validated dashboard (spec patches)."""
        return await self._store.save(dashboard)

    async def delete(self, dashboard_id: str) -> None:
        deleted = await self._store.delete(dashboard_id)
        if not deleted:
            raise DashboardNotFoundError(dashboard_id)

    # ------------------------------------------------------------------
    # Component management
    # ------------------------------------------------------------------

    async def add_component(
        self, dashboard_id: str, req: AddComponentRequest
    ) -> Dashboard:
        dashboard = await self.get(dashboard_id)
        component = req.component.to_domain()
        updated = dashboard.add_component(component)
        if component.visual is not None:
            updated = updated.model_copy(update={"spec_version": max(updated.spec_version, 2)})
            _check_spec(updated)
        return await self._store.save(updated)

    async def remove_component(self, dashboard_id: str, component_id: str) -> Dashboard:
        dashboard = await self.get(dashboard_id)
        if not any(c.id == component_id for c in dashboard.components):
            raise ComponentNotFoundError(dashboard_id, component_id)
        updated = dashboard.remove_component(component_id)
        return await self._store.save(updated)

    async def update_component(
        self, dashboard_id: str, component_id: str, req: UpdateComponentRequest
    ) -> Dashboard:
        dashboard = await self.get(dashboard_id)
        component = next(
            (c for c in dashboard.components if c.id == component_id), None
        )
        if component is None:
            raise ComponentNotFoundError(dashboard_id, component_id)
        if component.key_value is None:
            raise ValueError(
                "Only key_value components can be edited via this endpoint"
            )
        updated_component = component.model_copy(
            update={"key_value": req.key_value.to_domain()}
        )
        new_components = [
            updated_component if c.id == component_id else c
            for c in dashboard.components
        ]
        updated = dashboard.model_copy(
            update={
                "components": new_components,
                "updated_at": datetime.now(timezone.utc),
                "version": dashboard.version + 1,
            }
        )
        return await self._store.save(updated)

    async def move_component(
        self, dashboard_id: str, component_id: str, req: MoveComponentRequest
    ) -> Dashboard:
        dashboard = await self.get(dashboard_id)
        component = next(
            (c for c in dashboard.components if c.id == component_id), None
        )
        if component is None:
            raise ComponentNotFoundError(dashboard_id, component_id)

        new_position = req.position.to_domain()
        moved = component.model_copy(update={"position": new_position})
        new_components = [
            moved if c.id == component_id else c for c in dashboard.components
        ]
        updated = dashboard.model_copy(
            update={
                "components": new_components,
                "updated_at": datetime.now(timezone.utc),
                "version": dashboard.version + 1,
            }
        )
        return await self._store.save(updated)

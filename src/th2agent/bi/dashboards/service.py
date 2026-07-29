"""
dashboard/service.py
--------------------
Business logic layer: DashboardService + persistence protocol.

Swap InMemoryDashboardStore for any async ORM/DB adapter by implementing
the DashboardStore protocol — nothing else in the stack changes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

from th2agent.bi.dashboards.core import (
    Dashboard,
    DashboardStatus,
    DashboardVisibility,
)
from th2agent.bi.dashboards.schema import (
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
        )
        return await self._store.save(dashboard)

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
        return await self._store.save(updated)

    async def publish(self, dashboard_id: str, visibility: DashboardVisibility = DashboardVisibility.PRIVATE) -> Dashboard:
        dashboard = await self.get(dashboard_id)
        published = dashboard.publish(visibility=visibility)
        return await self._store.save(published)

    async def unpublish(self, dashboard_id: str) -> Dashboard:
        dashboard = await self.get(dashboard_id)
        unpublished = dashboard.unpublish()
        return await self._store.save(unpublished)

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

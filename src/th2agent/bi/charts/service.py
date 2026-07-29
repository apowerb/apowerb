"""
service.py
----------
ChartService: business logic layer between the router and the data store.

The store is intentionally an injected dict here so you can swap it for
SQLAlchemy, Redis, or any async ORM without touching the router or schemas.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol
from th2agent.bi.charts.core import Chart, ChartType, Filter, Theme
from th2agent.bi.charts.schemas import (
    ChartCreateRequest,
    ChartUpdateRequest,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
# BIMetadataService removed — DatabaseChartStore writes directly to the
# business_intelligence table, so a separate metadata layer is redundant.
# ---------------------------------------------------------------------------
# Store protocol — swap for any async ORM / DB adapter
# ---------------------------------------------------------------------------


class ChartStore(Protocol):
    """Minimal async persistence interface."""

    async def get(self, chart_id: str) -> Chart | None: ...
    async def list(self, *, page: int, page_size: int) -> tuple[list[Chart], int]: ...
    async def list_all(self) -> list[Chart]: ...
    async def save(self, chart: Chart) -> Chart: ...
    async def delete(self, chart_id: str) -> bool: ...


# ---------------------------------------------------------------------------
# In-memory store (development / testing)
# ---------------------------------------------------------------------------


class InMemoryChartStore:
    """Thread-safe enough for a single-process dev server."""
    def __init__(self) -> None:
        self._data: dict[str, Chart] = {}

    async def get(self, chart_id: str) -> Chart | None:
        return self._data.get(chart_id)
    async def list_all(self) -> list[Chart]:
        return sorted(
            self._data.values(),
            key=lambda c: c.created_at,
            reverse=True,
        )
    async def list(self, *, page: int, page_size: int) -> tuple[list[Chart], int]:
        all_charts = await self.list_all()
        total = len(all_charts)
        start = (page - 1) * page_size
        return all_charts[start: start + page_size], total

    async def save(self, chart: Chart) -> Chart:
        self._data[chart.id] = chart
        return chart

    async def delete(self, chart_id: str) -> bool:
        if chart_id in self._data:
            del self._data[chart_id]
            return True
        return False

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ChartNotFoundError(Exception):
    def __init__(self, chart_id: str) -> None:
        self.chart_id = chart_id
        super().__init__(f"Chart '{chart_id}' not found")


class ChartPermissionError(Exception):
    def __init__(self, chart_id: str, role: str) -> None:
        super().__init__(f"Role '{role}' cannot access chart '{chart_id}'")

class ChartConflictError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)

# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ChartService:
    """
    Orchestrates chart CRUD and enforces business rules.

    Example (FastAPI DI)
    --------------------
    store = InMemoryChartStore()
    service = ChartService(store)

    @app.get("/charts/{chart_id}")
    async def get_chart(chart_id: str, svc: ChartService = Depends(get_service)):
        return await svc.get(chart_id)
    """

    def __init__(self, store: ChartStore, db: AsyncSession | None = None) -> None:
        self._store = store
        self._db = db


    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------    ------------

    async def get(self, chart_id: str, *, viewer_role: str | None = None) -> Chart:
        """Fetch a single chart, optionally enforcing role visibility."""
        chart = await self._store.get(chart_id)
        if chart is None:
            raise ChartNotFoundError(chart_id)
        if viewer_role is not None and not chart.is_visible_to(viewer_role):
            raise ChartPermissionError(chart_id, viewer_role)
        return chart

    async def list(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        viewer_role: str | None = None,
    ) -> tuple[list[Chart], int]:
        """Return a paginated list of charts visible to the given role."""
        charts, total = await self._store.list(page=page, page_size=page_size)
        if viewer_role is not None:
            charts = [c for c in charts if c.is_visible_to(viewer_role)]
            total = len(charts)
        return charts, total

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    async def create(
        self,
        req: ChartCreateRequest,
        *,
        created_by: str | None = None,
    ) -> Chart:
        """Validate and persist a new chart."""
        chart = Chart.create(
            name=req.name,
            title=req.title,
            chart_type=req.chart_type,
            source=req.source.to_domain(),
            description=req.description,
            organization_id=req.organization_id,
            project_id=req.project_id,
            theme=req.theme.to_domain(),
            dimensions=req.dimensions.to_domain(),
            refresh_interval=req.refresh_interval,
            filters=[f.to_domain() for f in req.filters],
            permissions=req.permissions,
            created_by=created_by,
            config=req.config,
        )

        try:
            saved_chart = await self._store.save(chart)
        except IntegrityError as exc:
            raise ChartConflictError(
                f"A chart named '{req.name}' already exists in this organization."
            ) from exc
        return saved_chart

    async def update(self, chart_id: str, req: ChartUpdateRequest) -> Chart:
        """Apply a partial update (PATCH semantics)."""
        chart = await self._store.get(chart_id)
        if chart is None:
            raise ChartNotFoundError(chart_id)

        updates: dict = {"updated_at": datetime.now(timezone.utc)}

        if req.title is not None:
            updates["title"] = req.title
        if req.description is not None:
            updates["description"] = req.description
        if req.chart_type is not None:
            updates["chart_type"] = req.chart_type
        if req.source is not None:
            updates["source"] = req.source.to_domain()
        if req.theme is not None:
            updates["theme"] = req.theme.to_domain()
        if req.dimensions is not None:
            updates["dimensions"] = req.dimensions.to_domain()
        if req.refresh_interval is not None:
            updates["refresh_interval"] = req.refresh_interval
        if req.filters is not None:
            updates["filters"] = [f.to_domain() for f in req.filters]
        if req.permissions is not None:
            updates["permissions"] = req.permissions
        if req.name is not None:
            updates["name"] = req.name
        if req.organization_id is not None:
            updates["organization_id"] = req.organization_id
        if req.project_id is not None:
            updates["project_id"] = req.project_id
        if req.config is not None:
            updates["config"] = req.config

        updated_chart = chart.model_copy(update=updates)

        original_org = chart.organization_id
        original_project = chart.project_id
        moved_path = (
            original_org != updated_chart.organization_id
            or original_project != updated_chart.project_id
        )

        saved_chart = await self._store.save(updated_chart)
        return saved_chart
    
    async def delete(self, chart_id: str) -> None:
        chart = await self._store.get(chart_id)
        if chart is None:
            raise ChartNotFoundError(chart_id)

        deleted = await self._store.delete(chart_id)
        if not deleted:
            raise ChartNotFoundError(chart_id)

    # ------------------------------------------------------------------
    # Shortcuts (thin wrappers for common dashboard patterns)
    # ------------------------------------------------------------------

    async def create_kpi(
        self,
        name: str,
        title: str,
        query: str,
        *,
        organization_id: str,
        project_id: str = "thaink2",
        refresh_interval: int = 30,
        created_by: str | None = None,
    ) -> Chart:
        chart = Chart.kpi(
            title=title,
            query=query,
            refresh_interval=refresh_interval,
            created_by=created_by,
            name=name,
            organization_id=organization_id,
            project_id=project_id,
        )

        saved_chart = await self._store.save(chart)
        return saved_chart

    async def create_timeseries(
        self,
        name: str,
        title: str,
        query: str,
        time_field: str = "ts",
        *,
        organization_id: str,
        project_id: str = "thaink2",
        chart_type: ChartType = ChartType.LINE,
        refresh_interval: int = 60,
        created_by: str | None = None,
    ) -> Chart:
        chart = Chart.timeseries(
            name=name,
            title=title,
            query=query,
            time_field=time_field,
            chart_type=chart_type,
            organization_id=organization_id,
            project_id=project_id,
            refresh_interval=refresh_interval,
            created_by=created_by,
        )

        saved_chart = await self._store.save(chart)
        return saved_chart


    async def add_filter(self, chart_id: str, f: Filter) -> Chart:
        chart = await self.get(chart_id)
        updated = chart.with_filter(f)
        return await self._store.save(updated)

    async def change_theme(self, chart_id: str, theme: Theme) -> Chart:
        chart = await self.get(chart_id)
        updated = chart.with_theme(theme)
        return await self._store.save(updated)



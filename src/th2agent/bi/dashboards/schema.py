"""
dashboard/schema.py
-------------------
FastAPI request / response schemas for the Dashboard API.
Kept separate from core.py so the API contract evolves independently.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from th2agent.bi.dashboards.core import (
    ChartWidget,
    ColumnType,
    ComponentType,
    Dashboard,
    DashboardComponent,
    DashboardStatus,
    DashboardVisibility,
    GridPosition,
    KeyValue,
    TableColumn,
    TableWidget,
    Trend,
    TrendDirection,
    TrendSentiment,
)


# ---------------------------------------------------------------------------
# Component request schemas
# ---------------------------------------------------------------------------


class GridPositionSchema(BaseModel):
    row: int = Field(default=0, ge=0)
    col: int = Field(default=0, ge=0)
    width: int = Field(default=6, ge=1, le=12)
    height: int = Field(default=4, ge=1)

    def to_domain(self) -> GridPosition:
        return GridPosition(**self.model_dump())


class TrendSchema(BaseModel):
    value: float
    is_percentage: bool = True
    direction: TrendDirection
    sentiment: TrendSentiment
    label: str | None = None

    def to_domain(self) -> Trend:
        return Trend(**self.model_dump())


class KeyValueCreateSchema(BaseModel):
    label: str = Field(..., min_length=1, max_length=80)
    value: int | float | str
    unit: str | None = None
    description: str | None = None
    trend: TrendSchema | None = None
    source_query: str | None = None
    refresh_interval: int | None = Field(default=30, gt=0)

    def to_domain(self) -> KeyValue:
        return KeyValue(
            **{
                **self.model_dump(exclude={"trend"}),
                "trend": self.trend.to_domain() if self.trend else None,
            }
        )


class TableColumnSchema(BaseModel):
    key: str
    label: str
    col_type: ColumnType = ColumnType.STRING
    sortable: bool = True
    filterable: bool = False
    width: int | None = None
    format: str | None = None

    def to_domain(self) -> TableColumn:
        return TableColumn(**self.model_dump())


class TableWidgetCreateSchema(BaseModel):
    title: str = Field(..., min_length=1, max_length=120)
    description: str | None = None
    source_query: str
    columns: list[TableColumnSchema]
    default_page_size: int = Field(default=25, ge=1, le=500)
    default_sort_field: str | None = None
    searchable: bool = True
    refresh_interval: int | None = None

    def to_domain(self) -> TableWidget:
        return TableWidget(
            **{
                **self.model_dump(exclude={"columns"}),
                "columns": [c.to_domain() for c in self.columns],
            }
        )


class ChartWidgetSchema(BaseModel):
    chart_id: str
    title_override: str | None = None
    refresh_interval_override: int | None = Field(default=None, gt=0)

    def to_domain(self) -> ChartWidget:
        return ChartWidget(**self.model_dump())


class ComponentCreateSchema(BaseModel):
    """
    One component slot. Exactly one of chart / key_value / table must be set.
    component_type is required and must match the populated field.
    """

    component_type: ComponentType
    position: GridPositionSchema = Field(default_factory=GridPositionSchema)
    chart: ChartWidgetSchema | None = None
    key_value: KeyValueCreateSchema | None = None
    table: TableWidgetCreateSchema | None = None

    def to_domain(self) -> DashboardComponent:
        return DashboardComponent(
            component_type=self.component_type,
            position=self.position.to_domain(),
            chart=self.chart.to_domain() if self.chart else None,
            key_value=self.key_value.to_domain() if self.key_value else None,
            table=self.table.to_domain() if self.table else None,
        )


# ---------------------------------------------------------------------------
# Dashboard request schemas
# ---------------------------------------------------------------------------


class DashboardCreateRequest(BaseModel):
    """POST /dashboards"""

    title: str = Field(..., min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    slug: str | None = None
    columns: int = Field(default=12, ge=1, le=24)
    components: list[ComponentCreateSchema] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    agent_id: int | None = None

    model_config = {
        "json_schema_extra": {
            "example": {
                "title": "Agent operations",
                "slug": "agent-ops",
                "columns": 12,
                "components": [
                    {
                        "component_type": "chart",
                        "position": {"row": 0, "col": 0, "width": 8, "height": 4},
                        "chart": {"chart_id": "abc-123"},
                    },
                    {
                        "component_type": "key_value",
                        "position": {"row": 0, "col": 8, "width": 4, "height": 2},
                        "key_value": {
                            "label": "Total runs today",
                            "value": 2481,
                            "unit": "runs",
                        },
                    },
                ],
            }
        }
    }


class DashboardUpdateRequest(BaseModel):
    """PATCH /dashboards/{id} — all fields optional"""

    title: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None
    slug: str | None = None
    columns: int | None = Field(default=None, ge=1, le=24)
    status: DashboardStatus | None = None
    permissions: list[str] | None = None
    agent_id: int | None = None


class UpdateComponentRequest(BaseModel):
    """PATCH /dashboards/{id}/components/{component_id}.

    Edite le contenu d'une tuile key_value (chiffre cle). Les charts se
    modifient via leur propre endpoint chart.
    """

    key_value: KeyValueCreateSchema


class AddComponentRequest(BaseModel):
    """POST /dashboards/{id}/components"""

    component: ComponentCreateSchema


class MoveComponentRequest(BaseModel):
    """PATCH /dashboards/{id}/components/{component_id}/position"""

    position: GridPositionSchema


class PublishRequest(BaseModel):
    """POST /dashboards/{id}/publish"""

    visibility: DashboardVisibility = DashboardVisibility.PUBLIC


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class TrendResponse(BaseModel):
    value: float
    is_percentage: bool
    direction: TrendDirection
    sentiment: TrendSentiment
    label: str | None


class KeyValueResponse(BaseModel):
    id: str
    label: str
    value: int | float | str
    unit: str | None
    description: str | None
    trend: TrendResponse | None
    source_query: str | None
    refresh_interval: int | None


class TableColumnResponse(BaseModel):
    key: str
    label: str
    col_type: ColumnType
    sortable: bool
    filterable: bool
    width: int | None
    format: str | None


class TableWidgetResponse(BaseModel):
    id: str
    title: str
    description: str | None
    source_query: str
    columns: list[TableColumnResponse]
    default_page_size: int
    default_sort_field: str | None
    searchable: bool
    refresh_interval: int | None


class ChartWidgetResponse(BaseModel):
    chart_id: str
    title_override: str | None
    refresh_interval_override: int | None


class ComponentResponse(BaseModel):
    id: str
    component_type: ComponentType
    position: GridPosition
    chart: ChartWidgetResponse | None
    key_value: KeyValueResponse | None
    table: TableWidgetResponse | None

    model_config = {"from_attributes": True}


class DashboardResponse(BaseModel):
    """Full dashboard returned by the API."""

    id: str
    title: str
    description: str | None
    slug: str | None
    columns: int
    status: DashboardStatus
    visibility: DashboardVisibility
    version: int
    components: list[ComponentResponse]
    permissions: list[str]
    created_at: datetime
    updated_at: datetime
    created_by: str | None
    agent_id: int | None = None

    @classmethod
    def from_domain(cls, dashboard: Dashboard) -> "DashboardResponse":
        return cls(**dashboard.model_dump())

    model_config = {"from_attributes": True}


class DashboardSummaryResponse(BaseModel):
    """Lightweight item used in list responses."""

    id: str
    title: str
    description: str | None
    slug: str | None
    status: DashboardStatus
    visibility: DashboardVisibility
    version: int
    component_count: int
    created_by: str | None
    agent_id: int | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, d: Dashboard) -> "DashboardSummaryResponse":
        return cls(
            id=d.id,
            title=d.title,
            description=d.description,
            slug=d.slug,
            status=d.status,
            visibility=d.visibility,
            version=d.version,
            component_count=len(d.components),
            created_by=d.created_by,
            agent_id=d.agent_id,
            created_at=d.created_at,
            updated_at=d.updated_at,
        )


class DashboardListResponse(BaseModel):
    items: list[DashboardSummaryResponse]
    total: int
    page: int
    page_size: int
    has_next: bool

    @classmethod
    def paginate(
        cls,
        dashboards: list[Dashboard],
        *,
        page: int,
        page_size: int,
        total: int,
    ) -> "DashboardListResponse":
        return cls(
            items=[DashboardSummaryResponse.from_domain(d) for d in dashboards],
            total=total,
            page=page,
            page_size=page_size,
            has_next=(page * page_size) < total,
        )


class ErrorResponse(BaseModel):
    detail: str
    code: str | None = None

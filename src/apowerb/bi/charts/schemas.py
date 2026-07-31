"""
schemas.py
----------
FastAPI request / response schemas (Pydantic v2).
Kept separate from the domain model so the API contract can evolve
independently of the internal Chart entity.
"""

from __future__ import annotations

from datetime import datetime

from typing import Any

from pydantic import BaseModel, Field

from apowerb.bi.charts.core import (
    AggregationFunc,
    Chart,
    ChartType,
    DataSource,
    Dimensions,
    Filter,
    SortConfig,
    SourceType,
    Theme,
)


# ---------------------------------------------------------------------------
# Shared sub-schemas (re-exported for convenience in the router)
# ---------------------------------------------------------------------------

__all__ = [
    "DataSourceSchema",
    "FilterSchema",
    "SortConfigSchema",
    "ThemeSchema",
    "DimensionsSchema",
    "ChartCreateRequest",
    "ChartUpdateRequest",
    "ChartResponse",
    "ChartListResponse",
    "ErrorResponse",
]


class DataSourceSchema(BaseModel):
    source_type: SourceType = Field(default=SourceType.DATABASE, description="Source type: database, csv, google_drive, or agent")
    query: str = Field(default="", description="SQL snippet, query key, or endpoint path. Empty for agent sources.")
    aggregation: AggregationFunc | None = None
    group_by: list[str] = Field(default_factory=list)
    sort: SortConfig | None = None
    limit: int | None = Field(default=1000, gt=0, le=100_000)
    time_field: str | None = None
    connection_config_id: str | None = None
    source_options: dict[str, Any] = Field(default_factory=dict, description="Provider-specific options. For agent: {agent_ids: [1,2,3]}")

    def to_domain(self) -> DataSource:
        return DataSource(**self.model_dump())


class FilterSchema(BaseModel):
    field: str
    operator: str
    value: object = None

    def to_domain(self) -> Filter:
        return Filter(**self.model_dump())


class ThemeSchema(BaseModel):
    color_palette: list[str] = Field(
        default=["#6366f1", "#22d3ee", "#f59e0b", "#10b981", "#ef4444"]
    )
    font_family: str = "Inter, sans-serif"
    dark_mode: bool = False
    show_legend: bool = True
    show_grid: bool = True
    show_tooltip: bool = True

    def to_domain(self) -> Theme:
        return Theme(**self.model_dump())


class DimensionsSchema(BaseModel):
    width: int = Field(default=600, gt=0)
    height: int = Field(default=400, gt=0)
    responsive: bool = True

    def to_domain(self) -> Dimensions:
        return Dimensions(**self.model_dump())


class SortConfigSchema(BaseModel):
    field: str
    order: str = "desc"


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class ChartCreateRequest(BaseModel):
    """POST /charts"""

    name: str = Field(..., min_length=1, max_length=255)
    title: str = Field(..., min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    chart_type: ChartType
    source: DataSourceSchema
    theme: ThemeSchema = Field(default_factory=ThemeSchema)
    dimensions: DimensionsSchema = Field(default_factory=DimensionsSchema)
    refresh_interval: int | None = Field(default=None, gt=0)
    filters: list[FilterSchema] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    organization_id: str
    project_id: str = "thaink2"

    model_config = {
        "json_schema_extra": {
            "example": {
                "name": "sales_by_region",
                "title": "Sales by region",
                "organization_id": "th2example",
                "project_id": "thaink2",
                "chart_type": "bar",
                "source": {
                    "source_type": "database",
                    "query": "SELECT region, SUM(amount) FROM sales GROUP BY region",
                    "connection_config_id": "tool_config42",
                    "group_by": ["region"]
                },
                "refresh_interval": 60
            }
        }
    }


class ChartUpdateRequest(BaseModel):
    """PATCH /charts/{id} — all fields optional"""
    name: str | None = Field(default=None, min_length=1, max_length=255)
    title: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None
    chart_type: ChartType | None = None
    source: DataSourceSchema | None = None
    theme: ThemeSchema | None = None
    dimensions: DimensionsSchema | None = None
    refresh_interval: int | None = None
    filters: list[FilterSchema] | None = None
    permissions: list[str] | None = None
    config: dict[str, Any] | None = None
    organization_id: str | None = None
    project_id: str | None = None


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class ChartResponse(BaseModel):
    """Single chart returned by the API."""
    name: str = Field(..., min_length=1, max_length=255)
    id: str
    title: str
    description: str | None
    chart_type: ChartType
    source: DataSource
    theme: Theme
    dimensions: Dimensions
    refresh_interval: int | None
    filters: list[Filter]
    permissions: list[str]
    config: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    created_by: str | None
    organization_id: str
    project_id: str = "thaink2"

    @classmethod
    def from_domain(cls, chart: Chart) -> "ChartResponse":
        return cls(**chart.model_dump())

    model_config = {"from_attributes": True}


class ChartListResponse(BaseModel):
    """Paginated list of charts."""

    items: list[ChartResponse]
    total: int
    page: int
    page_size: int
    has_next: bool

    @classmethod
    def paginate(
        cls,
        charts: list[Chart],
        *,
        page: int,
        page_size: int,
        total: int,
    ) -> "ChartListResponse":
        return cls(
            items=[ChartResponse.from_domain(c) for c in charts],
            total=total,
            page=page,
            page_size=page_size,
            has_next=(page * page_size) < total,
        )


class ErrorResponse(BaseModel):
    detail: str
    code: str | None = None

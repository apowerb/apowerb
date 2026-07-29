"""
charts.py
---------
Core domain models: enums, supporting types, and the Chart entity.
No FastAPI or DB dependencies — pure Pydantic v2.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ChartType(str, Enum):
    BAR = "bar"
    LINE = "line"
    PIE = "pie"
    DONUT = "donut"
    SCATTER = "scatter"
    AREA = "area"
    HEATMAP = "heatmap"
    TABLE = "table"
    STAT = "stat"
    HISTOGRAM = "histogram"


class AggregationFunc(str, Enum):
    COUNT = "count"
    SUM = "sum"
    AVG = "avg"
    MIN = "min"
    MAX = "max"
    P50 = "p50"
    P95 = "p95"
    P99 = "p99"


class FilterOperator(str, Enum):
    EQ = "eq"
    NEQ = "neq"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    IN = "in"
    NOT_IN = "not_in"
    CONTAINS = "contains"
    IS_NULL = "is_null"
    IS_NOT_NULL = "is_not_null"


class SortOrder(str, Enum):
    ASC = "asc"
    DESC = "desc"

class SourceType(str, Enum):
    DATABASE = "database"
    CSV = "csv"
    GOOGLE_DRIVE = "google_drive"
    ONEDRIVE_EXCEL = "onedrive_excel"
    AGENT = "agent"


# ---------------------------------------------------------------------------
# Supporting models
# ---------------------------------------------------------------------------


class Dimensions(BaseModel):
    width: int = Field(default=600, gt=0)
    height: int = Field(default=400, gt=0)
    responsive: bool = Field(default=True)

    model_config = {"frozen": True}


class Theme(BaseModel):
    color_palette: list[str] = Field(
        default=["#6366f1", "#22d3ee", "#f59e0b", "#10b981", "#ef4444"]
    )
    font_family: str = Field(default="Inter, sans-serif")
    dark_mode: bool = False
    show_legend: bool = True
    show_grid: bool = True
    show_tooltip: bool = True

    @field_validator("color_palette")
    @classmethod
    def at_least_one_color(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("color_palette must contain at least one color")
        return v

    model_config = {"frozen": True}


class Filter(BaseModel):
    field: str
    operator: FilterOperator
    value: Any = None

    @model_validator(mode="after")
    def value_required_for_most_operators(self) -> "Filter":
        null_ops = {FilterOperator.IS_NULL, FilterOperator.IS_NOT_NULL}
        if self.operator not in null_ops and self.value is None:
            raise ValueError(f"operator '{self.operator}' requires a non-null value")
        return self

    model_config = {"frozen": True}


class SortConfig(BaseModel):
    field: str
    order: SortOrder = SortOrder.DESC

    model_config = {"frozen": True}


class DataSource(BaseModel):
    source_type: SourceType = Field(default=SourceType.DATABASE)
    query: str = Field(..., description="SQL snippet, query key, or endpoint path")
    aggregation: AggregationFunc | None = None
    group_by: list[str] = Field(default_factory=list)
    sort: SortConfig | None = None
    limit: int | None = Field(default=1000, gt=0, le=100_000)
    time_field: str | None = None
    connection_config_id: str | None = None
    source_options: dict[str, Any] = Field(default_factory=dict, description="Provider specific properties")

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# Chart entity
# ---------------------------------------------------------------------------


class Chart(BaseModel):
    """
    Core chart definition.

    Usage
    -----
    chart = Chart.create(
        title="Sales by region",
        chart_type=ChartType.BAR,
        source=DataSource(query="SELECT region, SUM(amount) FROM sales GROUP BY region",
                          connection_config_id="tool_config42"),
    )
    """

    # identity
    name: str = Field(..., min_length=1, max_length=255)
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    title: str = Field(..., min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    organization_id: str
    project_id: str = "thaink2"


    # core
    chart_type: ChartType
    source: DataSource

    # presentation
    theme: Theme = Field(default_factory=Theme)
    dimensions: Dimensions = Field(default_factory=Dimensions)

    # behaviour
    refresh_interval: int | None = Field(default=None, gt=0)
    filters: list[Filter] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)

    # free-form config (KPI column, aggregation, formula, unit, etc.)
    config: dict[str, Any] = Field(default_factory=dict)

    # meta
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    created_by: str | None = None

    # ------------------------------------------------------------------
    # Factories
    # ------------------------------------------------------------------

    @classmethod
    def create(
        cls,
        name: str,
        title: str,
        chart_type: ChartType,
        source: DataSource,
        *,
        description: str | None = None,
        theme: Theme | None = None,
        dimensions: Dimensions | None = None,
        refresh_interval: int | None = None,
        filters: list[Filter] | None = None,
        permissions: list[str] | None = None,
        created_by: str | None = None,
        organization_id: str,
        project_id: str = "thaink2",
        config: dict[str, Any] | None = None,
    ) -> "Chart":
        return cls(
            name=name,
            title=title,
            chart_type=chart_type,
            source=source,
            description=description,
            theme=theme or Theme(),
            dimensions=dimensions or Dimensions(),
            refresh_interval=refresh_interval,
            filters=filters or [],
            permissions=permissions or [],
            created_by=created_by,
            organization_id=organization_id,
            project_id=project_id,
            config=config or {},
        )

    @classmethod
    def timeseries(
        cls,
        title: str,
        query: str,
        time_field: str = "ts",
        *,
        chart_type: ChartType = ChartType.LINE,
        refresh_interval: int = 60,
        **kwargs: Any,
    ) -> "Chart":
        source = DataSource(query=query, time_field=time_field, group_by=[time_field])
        return cls.create(
            title=title,
            chart_type=chart_type,
            source=source,
            refresh_interval=refresh_interval,
            **kwargs,
        )

    @classmethod
    def kpi(
        cls,
        title: str,
        query: str,
        aggregation: AggregationFunc = AggregationFunc.COUNT,
        *,
        refresh_interval: int = 30,
        **kwargs: Any,
    ) -> "Chart":
        source = DataSource(query=query, aggregation=aggregation, limit=1)
        return cls.create(
            title=title,
            chart_type=ChartType.STAT,
            source=source,
            refresh_interval=refresh_interval,
            **kwargs,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def with_filter(self, f: Filter) -> "Chart":
        return self.model_copy(
            update={
                "filters": [*self.filters, f],
                "updated_at": datetime.now(timezone.utc),
            }
        )

    def with_theme(self, theme: Theme) -> "Chart":
        return self.model_copy(
            update={"theme": theme, "updated_at": datetime.now(timezone.utc)}
        )

    def is_visible_to(self, role: str) -> bool:
        return not self.permissions or role in self.permissions

    model_config = {"frozen": True}

"""
dashboard/core.py
-----------------
Pure domain models for the Dashboard entity and its three component types.
No FastAPI or DB dependencies.

Component types
---------------
ChartWidget   — embeds a Chart reference + display overrides
KeyValue      — a single KPI tile (label + value + optional trend)
TableWidget   — a tabular dataset with typed columns and pagination config

A Dashboard is a named, versioned container that holds an ordered list of
DashboardComponent references. Components are position-aware (row / col) so
the frontend can reconstruct the grid layout without extra config.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ComponentType(str, Enum):
    CHART = "chart"
    KEY_VALUE = "key_value"
    TABLE = "table"


class TrendDirection(str, Enum):
    UP = "up"
    DOWN = "down"
    NEUTRAL = "neutral"


class TrendSentiment(str, Enum):
    """Decoupled from direction — a drop can be good (e.g. error rate)."""

    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"


class ColumnType(str, Enum):
    STRING = "string"
    NUMBER = "number"
    BOOLEAN = "boolean"
    DATETIME = "datetime"
    BADGE = "badge"  # coloured status pill
    LINK = "link"


class DashboardStatus(str, Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class DashboardVisibility(str, Enum):
    PRIVATE = "private"
    ORGANIZATION = "organization"
    PUBLIC = "public"


# ---------------------------------------------------------------------------
# Shared primitives
# ---------------------------------------------------------------------------


class GridPosition(BaseModel):
    """Where a component sits in the dashboard grid."""

    row: int = Field(default=0, ge=0)
    col: int = Field(default=0, ge=0)
    width: int = Field(default=6, ge=1, le=12, description="Column span (1-12)")
    height: int = Field(default=4, ge=1, description="Row span in grid units")

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# Component: ChartWidget
# ---------------------------------------------------------------------------


class ChartWidget(BaseModel):
    """
    A reference to an existing Chart with optional display overrides.

    The chart_id points to a Chart defined in charts.py / service.py.
    Override fields here take precedence over the Chart's own config
    when rendering — useful for embedding the same chart in multiple
    dashboards with different titles or refresh rates.
    """

    chart_id: str = Field(..., description="ID of the referenced Chart entity")
    title_override: str | None = Field(
        default=None,
        description="Optional title override for this dashboard slot",
    )
    refresh_interval_override: int | None = Field(
        default=None,
        gt=0,
        description="Override the chart's own refresh_interval for this embed",
    )

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# Component: KeyValue
# ---------------------------------------------------------------------------


class Trend(BaseModel):
    """Trend metadata shown alongside a KPI value."""

    value: float = Field(..., description="Delta or percentage change")
    is_percentage: bool = True
    direction: TrendDirection
    sentiment: TrendSentiment
    label: str | None = Field(default=None, description="e.g. 'vs last 7 days'")

    model_config = {"frozen": True}


class KeyValue(BaseModel):
    """
    A single KPI tile.

    Example
    -------
    KeyValue(
        label="Total runs today",
        value=2_481,
        unit="runs",
        trend=Trend(value=12.4, direction=TrendDirection.UP,
                    sentiment=TrendSentiment.POSITIVE, label="vs yesterday"),
    )
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    label: str = Field(..., min_length=1, max_length=80)
    value: int | float | str
    unit: str | None = None
    description: str | None = None
    trend: Trend | None = None
    # query used to hydrate this tile at runtime
    source_query: str | None = Field(
        default=None,
        description="Query key / SQL resolved by the data layer",
    )
    refresh_interval: int | None = Field(default=30, gt=0)

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# Component: TableWidget
# ---------------------------------------------------------------------------


class TableColumn(BaseModel):
    """Metadata for a single table column."""

    key: str = Field(..., description="Field name in the data row dict")
    label: str = Field(..., description="Display header")
    col_type: ColumnType = ColumnType.STRING
    sortable: bool = True
    filterable: bool = False
    width: int | None = Field(default=None, description="Pixel hint for the frontend")
    format: str | None = Field(
        default=None,
        description="Optional format string, e.g. '%.2f', 'YYYY-MM-DD'",
    )

    model_config = {"frozen": True}


class TableWidget(BaseModel):
    """
    A tabular dataset definition.

    Column schema is defined here; actual rows are fetched via the data layer
    using source_query — same pipeline as ChartWidget.

    Example
    -------
    TableWidget(
        title="Recent orders",
        source_query="SELECT order_id, customer, status, total FROM orders ORDER BY created_at DESC",
        columns=[
            TableColumn(key="order_id",  label="Order ID"),
            TableColumn(key="customer",  label="Customer"),
            TableColumn(key="status",    label="Status",  col_type=ColumnType.BADGE),
            TableColumn(key="total",     label="Total",   col_type=ColumnType.NUMBER),
        ],
        default_page_size=25,
    )
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    title: str = Field(..., min_length=1, max_length=120)
    description: str | None = None
    source_query: str = Field(..., description="Query key resolved by the data layer")
    columns: list[TableColumn] = Field(default_factory=list)
    default_page_size: int = Field(default=25, ge=1, le=500)
    default_sort_field: str | None = None
    searchable: bool = True
    refresh_interval: int | None = None

    @model_validator(mode="after")
    def at_least_one_column(self) -> "TableWidget":
        if not self.columns:
            raise ValueError("TableWidget must define at least one column")
        return self

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# DashboardComponent — union wrapper
# ---------------------------------------------------------------------------


class DashboardComponent(BaseModel):
    """
    One slot in a dashboard grid.

    Exactly one of chart / key_value / table must be set.
    component_type is derived automatically by the validator.
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    component_type: ComponentType
    position: GridPosition = Field(default_factory=GridPosition)

    # exactly one of these is populated
    chart: ChartWidget | None = None
    key_value: KeyValue | None = None
    table: TableWidget | None = None

    @model_validator(mode="after")
    def exactly_one_component(self) -> "DashboardComponent":
        populated = [
            f for f in ("chart", "key_value", "table") if getattr(self, f) is not None
        ]
        if len(populated) != 1:
            raise ValueError(
                f"Exactly one component field must be set; got {populated or 'none'}"
            )
        expected = {
            "chart": ComponentType.CHART,
            "key_value": ComponentType.KEY_VALUE,
            "table": ComponentType.TABLE,
        }
        if expected[populated[0]] != self.component_type:
            raise ValueError(
                f"component_type '{self.component_type}' does not match "
                f"populated field '{populated[0]}'"
            )
        return self

    # ------------------------------------------------------------------
    # Factories
    # ------------------------------------------------------------------

    @classmethod
    def from_chart(
        cls,
        chart_id: str,
        *,
        position: GridPosition | None = None,
        title_override: str | None = None,
        refresh_interval_override: int | None = None,
    ) -> "DashboardComponent":
        return cls(
            component_type=ComponentType.CHART,
            position=position or GridPosition(),
            chart=ChartWidget(
                chart_id=chart_id,
                title_override=title_override,
                refresh_interval_override=refresh_interval_override,
            ),
        )

    @classmethod
    def from_key_value(
        cls,
        key_value: KeyValue,
        *,
        position: GridPosition | None = None,
    ) -> "DashboardComponent":
        return cls(
            component_type=ComponentType.KEY_VALUE,
            position=position or GridPosition(),
            key_value=key_value,
        )

    @classmethod
    def from_table(
        cls,
        table: TableWidget,
        *,
        position: GridPosition | None = None,
    ) -> "DashboardComponent":
        return cls(
            component_type=ComponentType.TABLE,
            position=position or GridPosition(),
            table=table,
        )

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# Dashboard entity
# ---------------------------------------------------------------------------


class Dashboard(BaseModel):
    """
    A named, versioned grid of components.

    Usage
    -----
    db = Dashboard.create(
        title="Agent operations",
        components=[
            DashboardComponent.from_chart("chart-abc", position=GridPosition(row=0, col=0, width=8)),
            DashboardComponent.from_key_value(kv, position=GridPosition(row=0, col=8, width=4)),
        ],
    )
    """

    # identity
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    title: str = Field(..., min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    slug: str | None = Field(
        default=None,
        description="URL-friendly identifier, e.g. 'agent-ops'",
    )

    # content
    components: list[DashboardComponent] = Field(default_factory=list)

    # layout
    columns: int = Field(default=12, ge=1, le=24, description="Grid column count")

    # lifecycle
    status: DashboardStatus = DashboardStatus.DRAFT
    visibility: DashboardVisibility = DashboardVisibility.PRIVATE
    version: int = Field(default=1, ge=1)

    # access control
    permissions: list[str] = Field(
        default_factory=list,
        description="Role names allowed to view. Empty = public.",
    )

    # ownership / scoping
    organization_id: str = Field(default="", description="Organization scope (e.g. email domain)")
    project_id: str = Field(default="", description="Project scope")

    # meta
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    created_by: str | None = None
    agent_id: int | None = None

    # ------------------------------------------------------------------
    # Factories
    # ------------------------------------------------------------------

    @classmethod
    def create(
        cls,
        title: str,
        *,
        description: str | None = None,
        slug: str | None = None,
        components: list[DashboardComponent] | None = None,
        columns: int = 12,
        permissions: list[str] | None = None,
        created_by: str | None = None,
        agent_id: int | None = None,
    ) -> "Dashboard":
        return cls(
            title=title,
            description=description,
            slug=slug,
            components=components or [],
            columns=columns,
            permissions=permissions or [],
            created_by=created_by,
            agent_id=agent_id,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def add_component(self, component: DashboardComponent) -> "Dashboard":
        """Return a new Dashboard with the component appended (immutable)."""
        return self.model_copy(
            update={
                "components": [*self.components, component],
                "updated_at": datetime.now(timezone.utc),
                "version": self.version + 1,
            }
        )

    def remove_component(self, component_id: str) -> "Dashboard":
        """Return a new Dashboard with the component removed (immutable)."""
        return self.model_copy(
            update={
                "components": [c for c in self.components if c.id != component_id],
                "updated_at": datetime.now(timezone.utc),
                "version": self.version + 1,
            }
        )

    def publish(self, visibility: DashboardVisibility = DashboardVisibility.PRIVATE) -> "Dashboard":
        import re
        slug = self.slug or re.sub(r'[^a-z0-9]+', '-', self.title.lower()).strip('-')
        return self.model_copy(
            update={
                "status": DashboardStatus.PUBLISHED,
                "visibility": visibility,
                "slug": slug,
                "version": self.version + 1,
                "updated_at": datetime.now(timezone.utc),
            }
        )

    def unpublish(self) -> "Dashboard":
        return self.model_copy(
            update={
                "status": DashboardStatus.DRAFT,
                "visibility": DashboardVisibility.PRIVATE,
                "updated_at": datetime.now(timezone.utc),
            }
        )

    def is_visible_to(self, role: str) -> bool:
        return not self.permissions or role in self.permissions

    def components_of_type(self, t: ComponentType) -> list[DashboardComponent]:
        return [c for c in self.components if c.component_type == t]

    model_config = {"frozen": True}

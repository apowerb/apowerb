"""
data/schema.py
--------------
Request and response models for the chart data pipeline.
No business logic — pure Pydantic v2 contracts.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from apowerb.bi.charts.core import ChartType, Filter, FilterOperator, SortOrder


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------


class DataRequest(BaseModel):
    """
    Parameters controlling a single data fetch.
    Built by the router from query-string params and passed to ChartDataService.
    """

    page: int = Field(default=1, ge=1)
    # ``page_size = None`` means "let the service fall back to
    # ``chart.source.limit`` (operator-configured per chart, default
    # 1000). Capped at 100_000 to mirror DataSource.limit's max.
    page_size: int | None = Field(default=None, ge=1, le=100_000)

    # optional ad-hoc filter layered on top of the chart's built-in filters
    filter_field: str | None = None
    filter_op: FilterOperator | None = None
    filter_value: str | None = None

    # optional sort override (overrides chart.source.sort when set)
    sort_field: str | None = None
    sort_order: SortOrder = SortOrder.DESC

    def extra_filters(self) -> list[Filter]:
        """Return a validated Filter list from the ad-hoc query-param filter."""
        if self.filter_field and self.filter_op:
            return [
                Filter(
                    field=self.filter_field,
                    operator=self.filter_op,
                    value=self.filter_value,
                )
            ]
        return []


# ---------------------------------------------------------------------------
# Response
# ---------------------------------------------------------------------------


class PageMeta(BaseModel):
    page: int
    page_size: int
    total: int
    has_next: bool
    has_prev: bool


class ChartDataResponse(BaseModel):
    """
    Standard raw-data envelope returned to the frontend.

    The frontend maps this to an ECharts option — the backend never builds
    chart library configs.

    Shape
    -----
    labels  — column / field names (derived from first row keys)
    rows    — one dict per data point or table row
    pagination — page metadata
    """

    chart_id: str
    chart_type: ChartType
    title: str
    labels: list[str]
    rows: list[dict[str, Any]]
    pagination: PageMeta
    refresh_interval: int | None = None
    description: str | None = None
    config: dict[str, Any] | None = None
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = {
        "json_schema_extra": {
            "example": {
                "chart_id": "abc-123",
                "chart_type": "bar",
                "title": "Agent runs per hour",
                "labels": ["hour", "success", "failed"],
                "rows": [
                    {"hour": "00:00", "success": 120, "failed": 3},
                    {"hour": "01:00", "success": 98, "failed": 7},
                ],
                "pagination": {
                    "page": 1,
                    "page_size": 50,
                    "total": 24,
                    "has_next": False,
                    "has_prev": False,
                },
                "refresh_interval": 60,
                "generated_at": "2026-03-17T10:00:00Z",
            }
        }
    }

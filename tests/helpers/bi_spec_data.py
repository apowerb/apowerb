"""Synthetic data and spec builders for the dashboard spec v2 tests.

Nothing here comes from a customer file: five generic product families with
two to three years of weekly sales generated from a seeded RNG.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from apowerb.bi.dashboards.core import ComponentType, Dashboard, DashboardComponent, GridPosition
from apowerb.bi.spec.models import (
    FilterSpec,
    InventorySimStep,
    KpiSpec,
    MeasureSpec,
    NaiveForecastStep,
    SeriesSpec,
    SourceSpec,
    ThresholdSpec,
    VisualSpec,
)

FAMILIES = ["Alpha", "Bravo", "Charlie", "Delta", "Echo"]
OWNER = "owner@example.com"
DATASET_ID = "11111111-1111-1111-1111-111111111111"


def weekly_sales(weeks: int = 130, families: list[str] | None = None, seed: int = 7) -> pd.DataFrame:
    """Weekly sales per family: seasonal curve + noise, Mondays from 2023-01-02."""
    rng = np.random.default_rng(seed)
    fams = families or FAMILIES
    dates = pd.date_range("2023-01-02", periods=weeks, freq="7D")
    rows = []
    for i, fam in enumerate(fams):
        base = 100 + 40 * i
        season = 1 + 0.3 * np.sin(2 * np.pi * np.arange(weeks) / 52)
        sales = np.round(base * season * (1 + rng.normal(0, 0.08, weeks)))
        for d, s in zip(dates, sales):
            rows.append({"semaine": d, "famille": fam, "ventes": float(max(s, 0)), "prix": 5.0 + i})
    return pd.DataFrame(rows)


def dataset_source(name: str = "ventes", transforms: list | None = None, measures: list | None = None) -> SourceSpec:
    return SourceSpec(
        name=name,
        kind="dataset",
        dataset_id=DATASET_ID,
        transforms=transforms or [],
        measures=measures or [],
    )


def pipeline_source(name: str = "ventes") -> SourceSpec:
    """Dataset + naive forecast + simulated stock, with the usual measures."""
    return dataset_source(
        name,
        transforms=[
            NaiveForecastStep(date="semaine", value="ventes", by=["famille"], window=4, season_length=52),
            InventorySimStep(date="semaine", by=["famille"], demand="ventes", forecast="prevision", default_lot=10),
        ],
        measures=[
            MeasureSpec(name="ventes_tot", label="Ventes", expr="SUM(ventes)", format="compact"),
            MeasureSpec(name="prevu_tot", label="Prévu", expr="SUM(prevision)", format="compact"),
            MeasureSpec(
                name="nsl",
                label="Niveau de service",
                expr="1 - SUM(demande_non_servie) / NULLIF(SUM(ventes), 0)",
                format="percent",
                thresholds=ThresholdSpec(direction="higher_is_better", good=0.99, warn=0.95),
            ),
            MeasureSpec(name="stock_tot", label="Stock fin", expr="SUM(stock_fin)", format="integer"),
            MeasureSpec(name="nb_semaines", label="Semaines", expr="COUNT(DISTINCT semaine)", format="integer"),
            MeasureSpec(name="n", label="Lignes", expr="COUNT(*)", format="integer"),
        ],
    )


def default_filters() -> list[FilterSpec]:
    return [
        FilterSpec(id="periode", label="Période", field="semaine", type="relative_period", default="26w"),
        FilterSpec(id="famille", label="Famille", field="famille", type="select"),
    ]


def visual_component(visual: VisualSpec, *, component_id: str, row: int = 0) -> DashboardComponent:
    return DashboardComponent(
        id=component_id,
        component_type=ComponentType.VISUAL,
        position=GridPosition(row=row, col=0, width=6, height=4),
        visual=visual,
    )


def kpi(measure: str, **kw: Any) -> VisualSpec:
    return VisualSpec(type="kpi", source="ventes", kpi=KpiSpec(measure=measure, **kw))


def combo(*keys: str, x: str = "semaine", **kw: Any) -> VisualSpec:
    return VisualSpec(type="combo", source="ventes", x=x, series=[SeriesSpec(key=k) for k in keys], **kw)


def build_dashboard(
    visuals: dict[str, VisualSpec],
    *,
    source: SourceSpec | None = None,
    filters: list[FilterSpec] | None = None,
    dashboard_id: str = "dash-1",
) -> Dashboard:
    return Dashboard(
        id=dashboard_id,
        title="Test spec",
        spec_version=2,
        created_by=OWNER,
        sources=[source or pipeline_source()],
        filters=default_filters() if filters is None else filters,
        components=[visual_component(v, component_id=cid, row=i) for i, (cid, v) in enumerate(visuals.items())],
    )

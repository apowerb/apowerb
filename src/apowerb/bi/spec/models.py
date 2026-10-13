"""
bi/spec/models.py
-----------------
Dashboard spec v2: the dashboard owns its data.

A v2 dashboard declares named ``sources`` (a dataset, a SQL query or another
source, refined by ordered ``transforms``), named ``measures`` on each source,
global ``filters``, and ``visual`` components that point at a source and its
fields instead of carrying their own data source.

Everything here is a pure Pydantic contract: no I/O. The engine
(``bi/spec/engine.py``) computes a :class:`DashboardDataResponse` from a spec
and a filter state; the UI and the agent tools edit the spec through
:data:`PatchOp` operations.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

SPEC_VERSION = 2

# Names used as SQL identifiers (sources, fields, measures, filter ids).
IDENT = r"^[a-z_][a-z0-9_]{0,62}$"
Ident = Annotated[str, Field(pattern=IDENT)]


# ---------------------------------------------------------------------------
# Fields and measures
# ---------------------------------------------------------------------------


class Provenance(str, Enum):
    REAL = "real"
    SIMULATED = "simulated"
    FORECAST = "forecast"


class FieldSpec(BaseModel):
    """Declared or inferred column of a source."""

    name: Ident
    label: str | None = None
    type: Literal["string", "number", "date", "boolean"] = "string"
    role: Literal["dimension", "measure", "date"] | None = None
    provenance: Provenance = Provenance.REAL
    format: str | None = None  # see MeasureSpec.format


class ThresholdSpec(BaseModel):
    """Traffic-light rule. ``higher_is_better``: value >= good → good,
    value >= warn → warn, else bad. ``lower_is_better`` mirrors it
    (value <= good → good, value <= warn → warn, else bad)."""

    direction: Literal["higher_is_better", "lower_is_better"] = "higher_is_better"
    good: float
    warn: float


class MeasureSpec(BaseModel):
    """Named aggregate expression evaluated by DuckDB over the filtered frame.

    ``expr`` is a SQL aggregate over the source columns, e.g.
    ``SUM(ventes)`` or ``1 - SUM(demande_non_servie) / NULLIF(SUM(ventes), 0)``.
    Inside ``expr`` the filtered source is available as the relation
    ``frame`` (for sub-queries such as ``(SELECT MAX(semaine) FROM frame)``).
    """

    name: Ident
    label: str
    expr: str = Field(..., min_length=1, max_length=2000)
    format: Literal["number", "integer", "compact", "percent", "decimal1", "decimal2", "weeks"] = "number"
    unit: str | None = None
    description: str | None = None
    thresholds: ThresholdSpec | None = None


# ---------------------------------------------------------------------------
# Transforms (ordered, declarative)
# ---------------------------------------------------------------------------


class FilterStep(BaseModel):
    op: Literal["filter"] = "filter"
    where: str  # SQL boolean expression


class DeriveStep(BaseModel):
    op: Literal["derive"] = "derive"
    column: Ident
    expr: str  # SQL scalar or window expression
    provenance: Provenance | None = None  # default: inherited (simulated if any input is)


class AggregateStep(BaseModel):
    op: Literal["aggregate"] = "aggregate"
    group_by: list[Ident]
    aggregates: dict[str, str]  # output column -> SQL aggregate


class JoinStep(BaseModel):
    op: Literal["join"] = "join"
    source: Ident  # another source of the same dashboard
    on: list[Ident]
    how: Literal["left", "inner"] = "left"
    columns: list[Ident] | None = None  # None = all non-key columns


class SqlStep(BaseModel):
    """Free read-only step: ``query`` selects FROM ``input``."""

    op: Literal["sql"] = "sql"
    query: str


class NaiveForecastStep(BaseModel):
    """Transparent one-step-ahead forecast (the method shown to the client).

    For the first ``season_length`` periods of each group (no "same period last
    year" yet): mean of the previous ``window`` periods. Then: value of the same period last year × trend, where
    trend = mean(last ``window`` periods) / mean(same ``window`` periods last
    year), clamped to ``trend_bounds``. Adds column ``output``
    (provenance=forecast). Rows sorted by ``date`` within each ``by`` group.
    """

    op: Literal["naive_forecast"] = "naive_forecast"
    date: Ident
    value: Ident
    by: list[Ident] = Field(default_factory=list)
    output: Ident = "prevision"
    window: int = Field(default=4, ge=1, le=52)
    season_length: int = Field(default=52, ge=2, le=366)
    trend_bounds: tuple[float, float] = (0.8, 1.25)


class ForecastStep(BaseModel):
    """Future periods from the th2forecast engine, appended as new rows
    (``value`` left NULL, ``output`` filled, provenance=forecast)."""

    op: Literal["forecast"] = "forecast"
    date: Ident
    value: Ident
    by: list[Ident] = Field(default_factory=list)
    horizon: int = Field(default=8, ge=1, le=104)
    output: Ident = "prevision_moteur"
    frequency: str | None = None


class InventorySimStep(BaseModel):
    """Simulated stock and replenishment (provenance=simulated).

    Per ``by`` group, period by period:
      stock_securite = safety_factor × forecast(t)
      stock_debut(t) = stock_fin(t-1)          (t0: initial_cover × forecast)
      appro(t)       = commande(t - lead_time), lead_time >= 1 (an order placed
                       at the end of t cannot land in t) (partial_rate of deliveries arrive
                       at half, deterministic with ``seed``)
      ventes_servies = min(demand, stock_debut + appro)
      demande_non_servie = demand - ventes_servies
      stock_fin      = stock_debut + appro - ventes_servies
      commande(t)    = max(0, order_factor × forecast(t+1) + stock_securite
                       - stock_fin), rounded up to the lot of the group
    Output columns: stock_debut, appro, ventes_servies, demande_non_servie,
    stock_fin, stock_securite, commande.
    """

    op: Literal["inventory_sim"] = "inventory_sim"
    date: Ident
    by: list[Ident] = Field(default_factory=list)
    demand: Ident = "ventes"
    forecast: Ident = "prevision"
    safety_factor: float = Field(default=0.5, ge=0)
    order_factor: float = Field(default=1.4, ge=0)
    lead_time: int = Field(default=1, ge=1, le=12)
    lot_sizes: dict[str, int] = Field(default_factory=dict)  # group key -> lot
    lot_column: Ident | None = None  # column whose value keys lot_sizes
    default_lot: int = Field(default=100, ge=1)
    partial_rate: float = Field(default=0.1, ge=0, le=1)
    initial_cover: float = Field(default=1.0, ge=0)
    seed: int = 42


class ReplenishmentPlanStep(BaseModel):
    """Projected stock and orders to place over the forecast horizon, from the
    REAL stock of each ``by`` group.

    Starts from the last row with a ``stock`` value. The future rows are the
    following rows with a ``forecast`` value (e.g. appended by a ``forecast``
    step); without any, ``horizon`` periods are appended with the mean of the
    last 4 periods of ``demand`` and a warning. For each future period t:
      reception(t) = orders placed at t - lead time
      stock(t)     = max(0, stock(t-1) + reception(t) - forecast(t))
      order at t if stock(t + lead time) would fall under the threshold:
      quantity = threshold + target_cover x forecast - stock(t + lead time),
      rounded up to the lot; no order that would land after the horizon.
    Threshold and lead time come from ``safety_column``/``lead_time_column``
    (last real value of the group), else ``safety_weeks`` x mean forecast and
    ``lead_time``. Pending orders are unknown: nothing arrives during the
    first lead-time periods.
    Output columns: stock_projete (real stock on the kept history rows),
    stock_sans_commande, reception, commande, seuil_plan, delai_plan,
    risque (2 = under the threshold despite the orders, 1 = order placed,
    0 = fine) and semaine_plan (0 = last real period, 1..H = horizon).
    """

    op: Literal["replenishment_plan"] = "replenishment_plan"
    date: Ident
    by: list[Ident] = Field(default_factory=list)
    stock: Ident
    demand: Ident = "ventes"
    forecast: Ident = "prevision_moteur"
    safety_column: Ident | None = None
    lead_time_column: Ident | None = None
    safety_weeks: float = Field(default=1.0, ge=0)
    lead_time: int = Field(default=1, ge=1, le=12)
    target_cover: int = Field(default=4, ge=0, le=26)
    horizon: int = Field(default=8, ge=1, le=104)
    keep_history: int = Field(default=12, ge=0, le=520)
    lot_sizes: dict[str, int] = Field(default_factory=dict)  # group key -> lot
    default_lot: int = Field(default=1, ge=1)


TransformStep = Annotated[
    Union[
        FilterStep,
        DeriveStep,
        AggregateStep,
        JoinStep,
        SqlStep,
        NaiveForecastStep,
        ForecastStep,
        InventorySimStep,
        ReplenishmentPlanStep,
    ],
    Field(discriminator="op"),
]


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


class SourceSpec(BaseModel):
    """A named data source owned by the dashboard. Rows are never embedded:
    ``kind=dataset`` → ``dataset_id`` (a BI data pool file),
    ``kind=sql`` → ``connection_config_id`` + ``query`` (SELECT only),
    ``kind=connector`` → any chart data source (``source_type`` + ``query``,
    ``connection_config_id``, ``source_options``: database, Google Drive,
    OneDrive, agent), read through the chart executors,
    ``kind=source`` → ``parent`` (another source of this dashboard)."""

    name: Ident
    label: str | None = None
    kind: Literal["dataset", "sql", "connector", "source"]
    dataset_id: str | None = None
    sheet: str | None = None
    connection_config_id: str | None = None
    query: str | None = None
    source_type: str | None = None  # connector: a chart SourceType value
    source_options: dict[str, Any] = Field(default_factory=dict)  # connector: provider properties
    parent: Ident | None = None
    transforms: list[TransformStep] = Field(default_factory=list)
    fields: list[FieldSpec] = Field(default_factory=list)  # overrides of inferred fields
    measures: list[MeasureSpec] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Global filters
# ---------------------------------------------------------------------------


class FilterSpec(BaseModel):
    """Dashboard-wide filter. Applies to every source that has ``field``
    unless ``applies_to`` narrows it.

    ``relative_period`` values: ``"13w"``, ``"26w"``, ``"52w"``, ``"3m"``,
    ``"12m"``, ``"all"`` — resolved against the latest date of the data.
    ``select`` value ``None`` means "all"."""

    id: Ident
    label: str
    field: Ident
    type: Literal["select", "multi", "relative_period", "date_range"] = "select"
    default: Any = None
    applies_to: list[Ident] | None = None
    presets: list[str] | None = None  # relative_period choices shown in the UI


# ---------------------------------------------------------------------------
# Visual components
# ---------------------------------------------------------------------------

SemanticColor = Literal["good", "warn", "bad", "muted", "accent"]


class ColorRule(BaseModel):
    """Colour a mark when ``value op threshold``. ``color``: semantic name or #hex."""

    op: Literal[">", ">=", "<", "<=", "==", "!="]
    value: float
    color: str


class SeriesSpec(BaseModel):
    key: Ident  # measure name (aggregated per x) or field name
    label: str | None = None
    mark: Literal["bar", "line", "area"] = "bar"
    color: str | None = None  # semantic name or #hex
    axis: Literal["left", "right"] = "left"
    dash: bool = False
    color_rules: list[ColorRule] = Field(default_factory=list)
    use_thresholds: bool = False  # colour each mark by the measure thresholds


class ReferenceLine(BaseModel):
    value: float | None = None
    measure: Ident | None = None  # scalar measure over the whole frame
    label: str | None = None
    color: str | None = "muted"
    dash: bool = True


class KpiSpec(BaseModel):
    measure: Ident
    compare: Literal["previous_period", "previous_point"] | None = None
    target_label: str | None = None  # e.g. "cible ≥ 99 %"
    secondary: str | None = None  # text template, see VisualSpec.text
    sparkline: Ident | None = None  # measure plotted per date over the real period


class ColumnSpec(BaseModel):
    key: Ident  # field (group key) or measure
    label: str | None = None
    format: str | None = None
    use_thresholds: bool = False
    # Measure evaluated for the table's total row instead of ``key`` — for
    # per-group measures whose overall value has another definition.
    total_measure: Ident | None = None


class WaterfallStep(BaseModel):
    label: str
    measure: Ident
    kind: Literal["start", "increase", "decrease", "end"]
    color: str | None = None


class ForecastVisualSpec(BaseModel):
    """Forecast widget bound to a dashboard source. The engine returns the
    filtered series; the widget computes the forecast like a forecast chart,
    so its other options (models, preprocessing, confidence_levels...) are
    kept as given."""

    model_config = ConfigDict(extra="allow")

    date_var: Ident
    target_var: Ident
    group_var: Ident | None = None
    horizon: int = Field(default=12, ge=1, le=366)
    frequency: str | None = None


class VisualSpec(BaseModel):
    """Inline visual bound to a dashboard source.

    Text templates (``text``, ``KpiSpec.secondary``, ``subtitle``) accept
    ``{{measure}}``, ``{{measure|percent}}`` (any MeasureSpec.format),
    ``{{filters.<id>}}`` (resolved label) and ``{{period.from}}``,
    ``{{period.to}}``, ``{{period.last}}``.
    """

    type: Literal[
        "kpi", "combo", "bar", "hbar", "line", "area", "pie",
        "diverging_bar", "waterfall", "table", "text", "note", "forecast",
    ]
    title: str | None = None
    subtitle: str | None = None
    source: Ident | None = None  # required except for note
    x: Ident | None = None  # dimension for charts and table rows
    series: list[SeriesSpec] = Field(default_factory=list)
    reference_lines: list[ReferenceLine] = Field(default_factory=list)
    sort: Literal["x_asc", "x_desc", "value_asc", "value_desc"] | None = None
    top_n: int | None = Field(default=None, ge=1, le=500)
    kpi: KpiSpec | None = None
    columns: list[ColumnSpec] = Field(default_factory=list)
    total_row: bool = False  # table: append an "all" row computed on the frame
    steps: list[WaterfallStep] = Field(default_factory=list)
    text: str | None = None  # text: template; note: markdown
    badges: list[Provenance] | None = None  # note: None = derived from source fields
    click_filters: dict[str, str] = Field(default_factory=dict)  # filter id -> x/row field
    ignore_filters: list[Ident] = Field(default_factory=list)
    highlight_filter: Ident | None = None  # table: highlight the row matching this filter
    forecast: ForecastVisualSpec | None = None  # forecast: series and widget options


# ---------------------------------------------------------------------------
# Data endpoint contract
# ---------------------------------------------------------------------------


class DashboardDataRequest(BaseModel):
    filters: dict[str, Any] = Field(default_factory=dict)  # filter id -> value
    components: list[str] | None = None  # None = all visual components


class KpiValue(BaseModel):
    value: float | None
    formatted: str
    status: Literal["good", "warn", "bad"] | None = None
    delta: float | None = None
    delta_formatted: str | None = None
    secondary: str | None = None
    target_label: str | None = None
    spark: list[float] | None = None  # kpi.sparkline: oldest first


class MeasureTrace(BaseModel):
    name: str
    label: str
    expr: str
    provenance: list[Provenance]


class ComponentMeta(BaseModel):
    source: str | None = None
    measures: list[MeasureTrace] = Field(default_factory=list)
    filters_applied: dict[str, Any] = Field(default_factory=dict)
    ignored_filters: list[str] = Field(default_factory=list)
    row_count: int = 0  # rows of the filtered frame
    provenance: list[Provenance] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)  # e.g. forecast engine unavailable


class ComponentResult(BaseModel):
    """Computed payload of one visual.

    rows — charts: one dict per x value: ``{x: ..., <series key>: number,
           "_color_<series key>": "good|warn|bad|#hex" (if coloured)}``;
           waterfall: ``{label, value, base, kind, color}``;
           table: one dict per row with columns, ``_status_<col>`` and
           ``_highlight``; pie: ``{x, value}``.
    value — kpi only. text — text (rendered) and note (markdown).
    reference_lines — resolved values.
    """

    type: str
    rows: list[dict[str, Any]] | None = None
    value: KpiValue | None = None
    text: str | None = None
    badges: list[Provenance] | None = None
    reference_lines: list[dict[str, Any]] = Field(default_factory=list)
    meta: ComponentMeta = Field(default_factory=ComponentMeta)
    error: str | None = None


class SourceMeta(BaseModel):
    fields: list[FieldSpec]
    row_count: int
    last_date: str | None = None
    warnings: list[str] = Field(default_factory=list)


class ResolvedFilter(BaseModel):
    value: Any = None
    label: str
    date_from: str | None = None
    date_to: str | None = None


class DashboardDataResponse(BaseModel):
    dashboard_id: str
    filters: dict[str, ResolvedFilter]
    filter_options: dict[str, list[Any]]  # select/multi: distinct values
    sources: dict[str, SourceMeta]
    components: dict[str, ComponentResult]
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Patch operations (shared by the UI and the agent tools)
# ---------------------------------------------------------------------------


class _Op(BaseModel):
    pass


class AddVisualOp(_Op):
    op: Literal["add_visual"] = "add_visual"
    visual: VisualSpec
    position: dict[str, int] | None = None  # {row, col, width, height}; None = append
    component_id: str | None = None


class UpdateVisualOp(_Op):
    op: Literal["update_visual"] = "update_visual"
    component_id: str
    changes: dict[str, Any]  # shallow merge into VisualSpec


class RemoveComponentOp(_Op):
    op: Literal["remove_component"] = "remove_component"
    component_id: str


class MoveComponentOp(_Op):
    op: Literal["move_component"] = "move_component"
    component_id: str
    position: dict[str, int]


class UpsertSourceOp(_Op):
    op: Literal["upsert_source"] = "upsert_source"
    source: SourceSpec


class RemoveSourceOp(_Op):
    op: Literal["remove_source"] = "remove_source"
    name: Ident


class UpsertMeasureOp(_Op):
    op: Literal["upsert_measure"] = "upsert_measure"
    source: Ident
    measure: MeasureSpec


class RemoveMeasureOp(_Op):
    op: Literal["remove_measure"] = "remove_measure"
    source: Ident
    name: Ident


class UpsertFilterOp(_Op):
    op: Literal["upsert_filter"] = "upsert_filter"
    filter: FilterSpec


class RemoveFilterOp(_Op):
    op: Literal["remove_filter"] = "remove_filter"
    id: Ident


class SetTitleOp(_Op):
    op: Literal["set_title"] = "set_title"
    title: str = Field(..., min_length=1, max_length=120)
    description: str | None = None


PatchOp = Annotated[
    Union[
        AddVisualOp,
        UpdateVisualOp,
        RemoveComponentOp,
        MoveComponentOp,
        UpsertSourceOp,
        RemoveSourceOp,
        UpsertMeasureOp,
        RemoveMeasureOp,
        UpsertFilterOp,
        RemoveFilterOp,
        SetTitleOp,
    ],
    Field(discriminator="op"),
]


class PatchRequest(BaseModel):
    ops: list[PatchOp] = Field(..., min_length=1, max_length=200)
    dry_run: bool = False  # validate + compute, do not save

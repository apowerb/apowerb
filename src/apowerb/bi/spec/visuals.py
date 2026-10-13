"""
bi/spec/visuals.py
------------------
Computation of one ``visual`` component from the filtered frame of its source.

The filtered source is exposed to DuckDB as the relation ``frame``; measures
are SQL aggregates over it (``MeasureSpec.expr``), series are measures (or
SUM of a field) aggregated per ``x`` value. Every value in the result is
JSON-safe (dates as ISO strings, NaN as ``None``).
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from apowerb.bi.spec import sql_guard
from apowerb.bi.spec.duck import SqlRunner, quote_ident, to_python
from apowerb.bi.spec.errors import SpecError
from apowerb.bi.spec.filters import AGGREGATION_SQL, ActiveFilter, apply_filters
from apowerb.bi.spec.formatting import format_date, format_delta, format_value
from apowerb.bi.spec.frames import MatSource, referenced_columns
from apowerb.bi.spec.models import (
    ColorRule,
    ColumnSpec,
    ComponentMeta,
    ComponentResult,
    KpiValue,
    MeasureSpec,
    MeasureTrace,
    Provenance,
    SeriesSpec,
    SourceSpec,
    ThresholdSpec,
    VisualSpec,
)
from apowerb.bi.spec.validate import TEMPLATE_TOKEN_RE

TOTAL_LABEL = "Toutes"
TOTAL_LABEL_EN = "All"
_RANK = {Provenance.REAL: 0, Provenance.SIMULATED: 1, Provenance.FORECAST: 2}
_DEFAULT_WATERFALL_COLORS = {"start": "accent", "increase": "good", "decrease": "bad", "end": "accent"}
# A measure that is exactly SUM(column): the aggregation filter may swap its function.
_PLAIN_SUM_RE = re.compile(r'^\s*SUM\s*\(\s*(?:"((?:[^"]|"")+)"|([A-Za-z_][A-Za-z0-9_]*))\s*\)\s*$', re.IGNORECASE)


@dataclass
class ComputeContext:
    """What a visual needs to be computed (one per ``compute_dashboard`` call)."""

    sources: dict[str, SourceSpec]
    mats: dict[str, MatSource]
    source_errors: dict[str, str]
    active: dict[str, ActiveFilter]
    runner: SqlRunner
    lang: str | None = None  # dashboard language; None = French
    _frames: dict[tuple[str, frozenset[str], bool], tuple[pd.DataFrame, dict[str, Any], list[str]]] = field(
        default_factory=dict
    )

    def frame(
        self, mat: MatSource, skip: frozenset[str], skip_periods: bool = False
    ) -> tuple[pd.DataFrame, dict[str, Any], list[str]]:
        key = (mat.name, skip, skip_periods)
        if key not in self._frames:
            self._frames[key] = apply_filters(mat, self.active, skip=skip, skip_periods=skip_periods)
        return self._frames[key]


def status_of(value: float | None, th: ThresholdSpec | None) -> str | None:
    """``good`` / ``warn`` / ``bad`` for a value (bounds are inclusive)."""
    if th is None or value is None:
        return None
    if th.direction == "higher_is_better":
        return "good" if value >= th.good else "warn" if value >= th.warn else "bad"
    return "good" if value <= th.good else "warn" if value <= th.warn else "bad"


def _rule_matches(rule: ColorRule, value: float) -> bool:
    return {
        ">": value > rule.value,
        ">=": value >= rule.value,
        "<": value < rule.value,
        "<=": value <= rule.value,
        "==": value == rule.value,
        "!=": value != rule.value,
    }[rule.op]


def _ordered(provs: set[Provenance]) -> list[Provenance]:
    return sorted(provs, key=lambda p: _RANK[p])


@dataclass
class _Series:
    key: str
    spec: SeriesSpec | None
    expr: str
    measure: MeasureSpec | None


class VisualBuilder:
    def __init__(self, ctx: ComputeContext, comp_id: str, visual: VisualSpec) -> None:
        self.ctx = ctx
        self.id = comp_id
        self.v = visual
        self.src: SourceSpec | None = None
        self.mat: MatSource | None = None
        self.df: pd.DataFrame = pd.DataFrame()
        self.applied: dict[str, Any] = {}
        self.ignored: list[str] = []
        self.measures: dict[str, MeasureSpec] = {}
        self.used_measures: dict[str, MeasureSpec] = {}
        self.used_columns: set[str] = set()
        self._scalar_cache: dict[str, float | None] = {}
        self._extra_warnings: list[str] = []

        if visual.source:
            if visual.source in ctx.source_errors:
                raise SpecError(f"Source « {visual.source} » indisponible : {ctx.source_errors[visual.source]}")
            if visual.source not in ctx.sources:
                raise SpecError(f"Source inconnue « {visual.source} ».")
            self.src = ctx.sources[visual.source]
            self.mat = ctx.mats[visual.source]
            self.measures = {m.name: m for m in self.src.measures}
            self.df, self.applied, self.ignored = ctx.frame(self.mat, frozenset(visual.ignore_filters))
        self.grains: dict[str, str] = {}  # date field -> time grain
        self.aggs: dict[str, str] = {}  # raw column -> aggregation keyword
        for fid, flt in ctx.active.items():
            if flt.value is None or self.mat is None or self.mat.name not in flt.sources:
                continue
            if fid in visual.ignore_filters:
                continue
            if flt.spec.type == "time_grain":
                self.grains[flt.spec.field] = flt.value
            elif flt.spec.type == "aggregation":
                self.aggs[flt.spec.field] = flt.value
        for name, m in list(self.measures.items()):
            plain = _PLAIN_SUM_RE.match(m.expr)
            col = plain and (plain.group(2) or plain.group(1).replace('""', '"'))
            if col and col in self.aggs:
                self.measures[name] = m.model_copy(update={"expr": self._agg(col)})

    # -- helpers -----------------------------------------------------------

    def measure(self, name: str) -> MeasureSpec:
        m = self.measures.get(name)
        if m is None:
            raise SpecError(f"Mesure inconnue « {name} » dans la source « {self.v.source} ».")
        self.used_measures[name] = m
        return m

    def _frame_columns(self) -> list[str]:
        return list(self.df.columns)

    def scalars(self, names: list[str], df: pd.DataFrame | None = None) -> dict[str, float | None]:
        """Value of each measure over the (filtered) frame, in one query."""
        frame = self.df if df is None else df
        wanted = list(dict.fromkeys(names))
        if df is None:
            wanted = [n for n in wanted if n not in self._scalar_cache]
        if wanted:
            parts = []
            for i, name in enumerate(wanted):
                expr = sql_guard.check_expression(self.measure(name).expr, f"Mesure « {name} »")
                parts.append(f"({expr}) AS _m{i}")
            res = self.ctx.runner.run(f"SELECT {', '.join(parts)} FROM frame", {"frame": frame})
            values = {
                n: _num(res.iloc[0][f"_m{i}"]) if len(res) else None for i, n in enumerate(wanted)
            }
            if df is not None:
                return {n: values.get(n) for n in names}
            self._scalar_cache.update(values)
        return {n: self._scalar_cache.get(n) for n in names}

    def _agg(self, column: str) -> str:
        """Aggregate of a raw column: SUM unless an aggregation filter says otherwise."""
        return f"{AGGREGATION_SQL[self.aggs.get(column, 'sum')]}({quote_ident(column)})"

    def _x_expr(self, x: str, frame: pd.DataFrame) -> str:
        """``x`` as grouped: bucketed by the active time grain when ``x`` is its date field."""
        qx = quote_ident(x)
        grain = self.grains.get(x)
        if grain is None:
            return qx
        if not pd.api.types.is_datetime64_any_dtype(frame[x]):
            warning = (f"Time grain ignored: “{x}” is not a date." if self.ctx.lang == "en"
                       else f"Grain temporel ignoré : « {x} » n'est pas une date.")
            if warning not in self._extra_warnings:
                self._extra_warnings.append(warning)
            return qx
        return f"date_trunc('{grain}', CAST({qx} AS TIMESTAMP))"

    def series_defs(self) -> list[_Series]:
        out: list[_Series] = []
        for s in self.v.series:
            if s.key in self.measures:
                m = self.measure(s.key)
                out.append(_Series(s.key, s, m.expr, m))
            else:
                if self.mat is None or s.key not in self.mat.df.columns:
                    raise SpecError(f"Série « {s.key} » : ni mesure ni champ de la source « {self.v.source} ».")
                self.used_columns.add(s.key)
                out.append(_Series(s.key, s, self._agg(s.key), None))
        return out

    def grouped(self, x: str, defs: list[tuple[str, str]], df: pd.DataFrame | None = None) -> pd.DataFrame:
        """``SELECT x, expr... FROM frame GROUP BY x`` → columns ``_x``, ``_c0``…"""
        frame = self.df if df is None else df
        if x not in frame.columns:
            raise SpecError(f"Champ x inconnu « {x} » dans la source « {self.v.source} ».")
        self.used_columns.add(x)
        qx = quote_ident(x)
        bx = self._x_expr(x, frame)
        parts = [f"{bx} AS _x"]
        for i, (_, expr) in enumerate(defs):
            parts.append(f"({sql_guard.check_expression(expr, 'Expression')}) AS _c{i}")
        sql = f"SELECT {', '.join(parts)} FROM frame WHERE {qx} IS NOT NULL GROUP BY {bx} ORDER BY {bx}"
        return self.ctx.runner.run(sql, {"frame": frame})

    # -- templates ---------------------------------------------------------

    def render(self, template: str | None) -> str | None:
        if template is None:
            return None
        tokens = [m.group(1) for m in TEMPLATE_TOKEN_RE.finditer(template)]
        names = [t for t in tokens if not t.startswith(("filters.", "period."))]
        values = self.scalars(names) if names else {}
        period: dict[str, str] | None = None

        def replace(match: Any) -> str:
            nonlocal period
            token, fmt = match.group(1), match.group(2)
            if token.startswith("filters."):
                fid = token.split(".", 1)[1]
                flt = self.ctx.active.get(fid)
                if flt is None:
                    raise SpecError(f"Filtre inconnu « {fid} » dans le gabarit.")
                return flt.public.label
            if token.startswith("period."):
                if period is None:
                    period = self.period()
                key = token.split(".", 1)[1]
                if key not in period:
                    raise SpecError(f"« {token} » inconnu dans le gabarit.")
                return period[key]
            m = self.measure(token)
            if fmt:
                return format_value(values.get(token), fmt, lang=self.ctx.lang)
            return format_value(values.get(token), m.format, m.unit, lang=self.ctx.lang)

        return TEMPLATE_TOKEN_RE.sub(replace, template)

    def period(self) -> dict[str, str]:
        """``{from, to, last}`` (dd/mm/yyyy) of the filtered frame."""
        if self.mat is None:
            return {"from": "", "to": "", "last": ""}
        col = self.mat.date_field()
        for flt in self.ctx.active.values():
            if flt.is_period and self.mat.name in flt.sources and flt.spec.field in self.df.columns:
                col = flt.spec.field
                break
        if col is None or col not in self.df.columns:
            return {"from": "", "to": "", "last": ""}
        dates = pd.to_datetime(self.df[col], errors="coerce")
        if not dates.notna().any():
            return {"from": "", "to": "", "last": ""}
        last = dataclasses.replace(self.mat, df=self.df).real_last_date(col)
        return {
            "from": format_date(dates.min(), self.ctx.lang),
            "to": format_date(dates.max(), self.ctx.lang),
            "last": format_date(last, self.ctx.lang),
        }

    # -- sorting -----------------------------------------------------------

    def order(self, df: pd.DataFrame, value_col: str | None, default: str) -> pd.DataFrame:
        sort = self.v.sort or default
        top = self.v.top_n
        if top and value_col:
            df = df.sort_values(value_col, ascending=False, na_position="last", kind="stable").head(top)
        if sort in ("value_asc", "value_desc") and value_col:
            df = df.sort_values(value_col, ascending=sort == "value_asc", na_position="last", kind="stable")
        elif sort in ("x_asc", "x_desc"):
            df = df.sort_values("_x", ascending=sort == "x_asc", kind="stable")
        return df

    # -- meta --------------------------------------------------------------

    def provenance_of_measure(self, m: MeasureSpec) -> list[Provenance]:
        if self.mat is None:
            return [Provenance.REAL]
        cols = referenced_columns(m.expr, self.mat.columns)
        provs = {self.mat.provenance_of(c) for c in cols}
        return _ordered(provs) or [Provenance.REAL]

    def meta(self) -> ComponentMeta:
        traces: list[MeasureTrace] = []
        provs: set[Provenance] = set()
        warnings: list[str] = list(self._extra_warnings)
        if self.mat is not None:
            for m in self.used_measures.values():
                p = self.provenance_of_measure(m)
                provs.update(p)
                traces.append(MeasureTrace(name=m.name, label=m.label, expr=m.expr, provenance=p))
                self.used_columns.update(referenced_columns(m.expr, self.mat.columns))
            for col in self.used_columns:
                if col in self.mat.columns:
                    provs.add(self.mat.provenance_of(col))
            for col, message in self.mat.failed_columns.items():
                if col in self.used_columns:
                    warnings.append(f"Forecast unavailable: {message}." if self.ctx.lang == "en"
                                    else f"Prévision indisponible : {message}.")
        return ComponentMeta(
            source=self.v.source,
            measures=traces,
            filters_applied=dict(self.applied),
            ignored_filters=list(self.ignored),
            row_count=int(len(self.df)),
            provenance=_ordered(provs),
            warnings=warnings,
        )

    # -- visual types ------------------------------------------------------

    def build(self) -> ComponentResult:
        builder = {
            "kpi": self.kpi,
            "pie": self.pie,
            "waterfall": self.waterfall,
            "table": self.table,
            "text": self.text,
            "note": self.note,
            "forecast": self.forecast,
        }.get(self.v.type, self.chart)
        result = builder()
        extra = self._reference_lines() if self.v.type in _CHART_TYPES else []
        result.reference_lines = extra
        result.meta = self.meta()
        return result

    def _reference_lines(self) -> list[dict[str, Any]]:
        lines = self.v.reference_lines
        if not lines:
            return []
        names = [ln.measure for ln in lines if ln.measure]
        values = self.scalars(names) if names else {}
        out: list[dict[str, Any]] = []
        for ln in lines:
            if ln.measure:
                m = self.measure(ln.measure)
                value = values.get(ln.measure)
                label = ln.label or m.label
            else:
                value, label = ln.value, ln.label
            out.append({"value": to_python(value), "label": label, "color": ln.color, "dash": ln.dash})
        return out

    def chart(self) -> ComponentResult:
        v = self.v
        if not v.x:
            raise SpecError("`x` est requis pour ce type de visuel.")
        defs = self.series_defs()
        if not defs:
            raise SpecError("Au moins une série est requise.")
        res = self.grouped(v.x, [(d.key, d.expr) for d in defs])
        res = self.order(res, "_c0", "x_asc")
        rows: list[dict[str, Any]] = []
        for _, r in res.iterrows():
            x = to_python(r["_x"])
            row: dict[str, Any] = {"x": x}
            if v.x not in {d.key for d in defs}:
                row[v.x] = x
            for i, d in enumerate(defs):
                val = _num(r[f"_c{i}"])
                row[d.key] = val
                color = self._color(val, d)
                if color:
                    row[f"_color_{d.key}"] = color
            rows.append(row)
        return ComponentResult(type=v.type, rows=rows)

    @staticmethod
    def _color(value: float | None, d: _Series) -> str | None:
        if value is None or d.spec is None:
            return None
        for rule in d.spec.color_rules:
            if _rule_matches(rule, value):
                return rule.color
        if d.spec.use_thresholds and d.measure is not None:
            return status_of(value, d.measure.thresholds)
        return None

    def pie(self) -> ComponentResult:
        v = self.v
        if not v.x:
            raise SpecError("`x` est requis pour un camembert.")
        defs = self.series_defs()
        if not defs:
            raise SpecError("Une série est requise pour un camembert.")
        res = self.grouped(v.x, [(defs[0].key, defs[0].expr)])
        res = self.order(res, "_c0", "value_desc")
        rows = [{"x": to_python(r["_x"]), "value": _num(r["_c0"])} for _, r in res.iterrows()]
        return ComponentResult(type="pie", rows=rows)

    def kpi(self) -> ComponentResult:
        k = self.v.kpi
        if k is None:
            raise SpecError("Bloc `kpi` manquant.")
        m = self.measure(k.measure)
        value = self.scalars([k.measure])[k.measure]
        delta: float | None = None
        delta_text: str | None = None
        current, previous = value, None
        if k.compare == "previous_period":
            previous = self._previous_period_value(k.measure)
        elif k.compare == "previous_point":
            last, previous = self._last_two_points(k.measure)
            if last is not None:
                current = last
        if previous is not None:
            delta, delta_text = format_delta(current, previous, m.format, lang=self.ctx.lang)
        kpi_value = KpiValue(
            value=value,
            formatted=format_value(value, m.format, m.unit, lang=self.ctx.lang),
            status=status_of(value, m.thresholds),  # type: ignore[arg-type]
            delta=delta,
            delta_formatted=delta_text,
            secondary=self.render(k.secondary),
            target_label=k.target_label,
            spark=self._spark(k.sparkline) if k.sparkline else None,
        )
        return ComponentResult(type="kpi", value=kpi_value)

    def _spark(self, measure: str) -> list[float] | None:
        """Measure per date, up to the last real date of the active period filter."""
        col = self._date_column()
        if col is None or self.mat is None:
            return None
        res = self.grouped(col, [(measure, self.measure(measure).expr)])
        lasts = [f.last for f in self.ctx.active.values()
                 if f.is_period and f.last is not None and self.mat.name in f.sources]
        if lasts:
            res = res[pd.to_datetime(res["_x"], errors="coerce") <= min(lasts)]
        points = [x for x in (_num(v) for v in res["_c0"]) if x is not None]
        return points if len(points) >= 2 else None

    def _date_column(self) -> str | None:
        if self.mat is None:
            return None
        if self.v.x and self.v.x in self.mat.df.columns and pd.api.types.is_datetime64_any_dtype(self.mat.df[self.v.x]):
            return self.v.x
        return self.mat.date_field()

    def _last_two_points(self, measure: str) -> tuple[float | None, float | None]:
        col = self._date_column()
        if col is None:
            return None, None
        res = self.grouped(col, [(measure, self.measure(measure).expr)])
        values = [_num(x) for x in res["_c0"]]
        values = [x for x in values if x is not None]
        if len(values) < 2:
            return None, None
        return values[-1], values[-2]

    def _previous_period_value(self, measure: str) -> float | None:
        assert self.mat is not None
        for flt in self.ctx.active.values():
            upper = flt.hi_incl if flt.hi_incl is not None else flt.last
            if not (flt.is_period and flt.lo_excl is not None and upper is not None):
                continue
            if self.mat.name not in flt.sources:
                continue
            length = upper - flt.lo_excl
            lo, hi = flt.lo_excl - length, flt.lo_excl
            base, _, _ = self.ctx.frame(self.mat, frozenset(self.v.ignore_filters), skip_periods=True)
            dates = pd.to_datetime(base[flt.spec.field], errors="coerce")
            window = base[(dates > lo) & (dates <= hi)]
            if window.empty:
                return None
            return self.scalars([measure], df=window)[measure]
        return None

    def waterfall(self) -> ComponentResult:
        steps = self.v.steps
        if not steps:
            raise SpecError("Au moins une étape est requise.")
        values = self.scalars([s.measure for s in steps])
        rows: list[dict[str, Any]] = []
        running = 0.0
        warnings: list[str] = []
        for s in steps:
            raw = values.get(s.measure)
            amount = float(raw) if raw is not None else 0.0
            if s.kind == "start":
                base, shown = 0.0, amount
                running = amount
            elif s.kind == "increase":
                base, shown = running, abs(amount)
                running += abs(amount)
            elif s.kind == "decrease":
                shown = abs(amount)
                running -= shown
                base = running
            else:  # end
                base, shown = 0.0, amount
                if abs(amount - running) > 1e-6 * max(1.0, abs(amount), abs(running)):
                    lang = self.ctx.lang
                    warnings.append(
                        f"The final total “{s.label}” ({format_value(amount, lang=lang)}) differs from the sum of the "
                        f"steps ({format_value(running, lang=lang)})."
                        if lang == "en"
                        else f"Le total final « {s.label} » ({format_value(amount)}) diffère du cumul des étapes "
                        f"({format_value(running)})."
                    )
                running = amount
            rows.append(
                {
                    "label": s.label,
                    "value": to_python(shown),
                    "base": to_python(base),
                    "kind": s.kind,
                    "color": s.color or _DEFAULT_WATERFALL_COLORS[s.kind],
                }
            )
        self._extra_warnings = warnings
        return ComponentResult(type="waterfall", rows=rows)

    def table(self) -> ComponentResult:
        v = self.v
        assert self.src is not None and self.mat is not None
        cols = list(v.columns)
        if not cols:
            if v.x:
                cols.append(ColumnSpec(key=v.x))
            cols.extend(ColumnSpec(key=m.name) for m in self.src.measures)
        if not cols:
            raise SpecError("Aucune colonne à afficher.")
        defs: list[tuple[str, str]] = []
        total_defs: list[tuple[str, str]] = []
        for c in cols:
            if c.key == v.x:
                continue
            if c.key in self.measures:
                defs.append((c.key, self.measure(c.key).expr))
            elif c.key in self.mat.df.columns:
                self.used_columns.add(c.key)
                numeric = pd.api.types.is_numeric_dtype(self.mat.df[c.key]) and not pd.api.types.is_bool_dtype(
                    self.mat.df[c.key]
                )
                defs.append((c.key, self._agg(c.key) if numeric else f"ANY_VALUE({quote_ident(c.key)})"))
            else:
                raise SpecError(f"Colonne « {c.key} » : ni mesure ni champ de la source « {v.source} ».")
            if c.total_measure:
                total_defs.append((c.total_measure, self.measure(c.total_measure).expr))
            else:
                total_defs.append(defs[-1])
        value_col = "_c0" if defs else None

        def make_row(x_value: Any, res_row: Any, total: bool = False) -> dict[str, Any]:
            row: dict[str, Any] = {}
            if v.x:
                row[v.x] = x_value
            offset = 0
            for c in cols:
                if c.key == v.x:
                    continue
                val = _num(res_row[f"_c{offset}"]) if _is_num(res_row[f"_c{offset}"]) else to_python(res_row[f"_c{offset}"])
                offset += 1
                row[c.key] = val
                m = self.measures.get(c.total_measure if total and c.total_measure else c.key)
                if c.use_thresholds and m is not None and m.thresholds is not None and isinstance(val, (int, float)):
                    status = status_of(float(val), m.thresholds)
                    if status:
                        row[f"_status_{c.key}"] = status
            if v.highlight_filter:
                row["_highlight"] = False if total else self._is_highlighted(x_value)
            if total:
                row["_total"] = True
            return row

        rows: list[dict[str, Any]] = []
        if v.x:
            res = self.grouped(v.x, defs) if defs else self.ctx.runner.run(
                f"SELECT DISTINCT {self._x_expr(v.x, self.df)} AS _x FROM frame WHERE {quote_ident(v.x)} IS NOT NULL",
                {"frame": self.df},
            )
            self.used_columns.add(v.x)
            res = self.order(res, value_col, "x_asc")
            rows = [make_row(to_python(r["_x"]), r) for _, r in res.iterrows()]
        elif defs:
            parts = ", ".join(f"({sql_guard.check_expression(e, 'Expression')}) AS _c{i}" for i, (_, e) in enumerate(defs))
            res = self.ctx.runner.run(f"SELECT {parts} FROM frame", {"frame": self.df})
            rows = [make_row(None, r) for _, r in res.iterrows()]
        if v.total_row and total_defs:
            parts = ", ".join(
                f"({sql_guard.check_expression(e, 'Expression')}) AS _c{i}" for i, (_, e) in enumerate(total_defs)
            )
            res = self.ctx.runner.run(f"SELECT {parts} FROM frame", {"frame": self.df})
            if len(res):
                rows.append(make_row(TOTAL_LABEL_EN if self.ctx.lang == "en" else TOTAL_LABEL, res.iloc[0], total=True))
        return ComponentResult(type="table", rows=rows)

    def _is_highlighted(self, x_value: Any) -> bool:
        flt = self.ctx.active.get(self.v.highlight_filter or "")
        if flt is None or flt.value is None or x_value is None or flt.is_period:
            return False
        wanted = flt.value if isinstance(flt.value, list) else [flt.value]
        return str(x_value) in {str(w) for w in wanted}

    def text(self) -> ComponentResult:
        if not self.v.text:
            raise SpecError("`text` manquant.")
        return ComponentResult(type="text", text=self.render(self.v.text))

    def forecast(self) -> ComponentResult:
        """The filtered series, sorted by group then date; the widget forecasts it."""
        from apowerb.schema.forecast_schema import MAX_DATA_ROWS

        f = self.v.forecast
        if f is None:
            raise SpecError("Bloc `forecast` manquant.")
        cols = [f.date_var, f.target_var] + ([f.group_var] if f.group_var else [])
        missing = [c for c in cols if c not in self.df.columns]
        if missing:
            raise SpecError(f"Champ inconnu dans la source « {self.v.source} » : {', '.join(missing)}.")
        self.used_columns.update(cols)
        frame = self.df[cols].dropna(subset=[f.date_var])
        order = ([f.group_var] if f.group_var else []) + [f.date_var]
        frame = frame.sort_values(order, kind="stable").head(MAX_DATA_ROWS)
        rows = [{c: to_python(v) for c, v in zip(cols, rec)} for rec in frame.itertuples(index=False, name=None)]
        return ComponentResult(type="forecast", rows=rows)

    def note(self) -> ComponentResult:
        badges = self.v.badges
        if badges is None:
            provs = {f.provenance for f in self.mat.fields} if self.mat is not None else set()
            badges = _ordered(provs)
        return ComponentResult(type="note", text=self.v.text or "", badges=badges)


_CHART_TYPES = ("combo", "bar", "hbar", "line", "area", "diverging_bar", "pie")


def _is_num(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    try:
        return not pd.isna(value) and isinstance(to_python(value), (int, float))
    except (TypeError, ValueError):
        return False


def _num(value: Any) -> float | None:
    """Float (or None) from a DuckDB / numpy scalar."""
    py = to_python(value)
    if py is None or isinstance(py, bool):
        return None
    if isinstance(py, (int, float)):
        return float(py)
    try:
        return float(py)
    except (TypeError, ValueError):
        return None


def build_component(ctx: ComputeContext, comp_id: str, visual: VisualSpec) -> ComponentResult:
    """Compute one visual; an error is reported on the component only."""
    builder: VisualBuilder | None = None
    try:
        builder = VisualBuilder(ctx, comp_id, visual)
        return builder.build()
    except SpecError as exc:
        return ComponentResult(type=visual.type, error=str(exc), meta=ComponentMeta(source=visual.source))
    except Exception as exc:  # noqa: BLE001 - never let one visual break the dashboard
        import logging

        logging.getLogger(__name__).exception("[BiSpec] visual %s failed: %s", comp_id, exc)
        return ComponentResult(
            type=visual.type,
            error="Erreur de calcul inattendue (voir les journaux du serveur).",
            meta=ComponentMeta(source=visual.source),
        )

"""
bi/spec/filters.py
------------------
Resolution and application of the dashboard-wide :class:`FilterSpec`.

A filter value comes from the request (``DashboardDataRequest.filters``) or,
failing that, from ``FilterSpec.default``. Period filters are resolved against
the latest date of their field in the data (forecast rows appended after the
last real value do not move it) and applied to each source that has the field.
Filters are applied with pandas masks, never by interpolating values in SQL.
``time_grain`` and ``aggregation`` filters restrict nothing: the visuals read
their (whitelisted) value to bucket dates or pick the aggregate function.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from apowerb.bi.spec.errors import SpecError
from apowerb.bi.spec.formatting import format_date_fr
from apowerb.bi.spec.frames import MatSource
from apowerb.bi.spec.models import AGGREGATIONS, TIME_GRAINS, FilterSpec, ResolvedFilter

_PRESET_RE = re.compile(r"^(\d{1,3})([dwmy])$")
_TO_DATE = ("ytd", "mtd")
_ALL_STRINGS = ("", "all", "__all__", "*")
RESHAPING = ("time_grain", "aggregation")
GRAIN_LABELS = {
    "hour": "Par heure",
    "day": "Par jour",
    "week": "Par semaine",
    "month": "Par mois",
    "quarter": "Par trimestre",
    "year": "Par année",
}
AGGREGATION_LABELS = {"sum": "Somme", "avg": "Moyenne", "min": "Minimum", "max": "Maximum", "count": "Nombre"}
AGGREGATION_SQL = {"sum": "SUM", "avg": "AVG", "min": "MIN", "max": "MAX", "count": "COUNT"}
assert set(GRAIN_LABELS) == set(TIME_GRAINS) and set(AGGREGATION_LABELS) == set(AGGREGATIONS)


@dataclass
class ActiveFilter:
    """A resolved filter, ready to be applied."""

    spec: FilterSpec
    public: ResolvedFilter
    value: Any = None  # normalised value (None = no restriction)
    # period filters: rows with lo_excl < date <= hi_incl
    lo_excl: pd.Timestamp | None = None
    hi_incl: pd.Timestamp | None = None
    last: pd.Timestamp | None = None  # relative_period: last real date (not a filter bound)
    sources: set[str] = field(default_factory=set)  # sources that have the field

    @property
    def is_period(self) -> bool:
        return self.spec.type in ("relative_period", "date_range")

    @property
    def restricts(self) -> bool:
        if self.spec.type in RESHAPING:
            return False
        if self.is_period:
            return self.lo_excl is not None or self.hi_incl is not None
        return self.value is not None


def _plural(word: str) -> str:
    return word if word.endswith(("s", "x")) else word + "s"


def _feminine(word: str) -> bool:
    return word.endswith(("e", "ion", "té"))


def all_label(label: str) -> str:
    """« Toutes familles » / « Tous produits » from the filter label."""
    word = label.strip().lower()
    return f"{'Toutes' if _feminine(word) else 'Tous'} {_plural(word)}"


def period_label(preset: str) -> str:
    if preset == "all":
        return "Toute la période"
    if preset == "ytd":
        return "Depuis le début de l'année"
    if preset == "mtd":
        return "Depuis le début du mois"
    m = _PRESET_RE.match(preset)
    if not m:
        return preset
    n, unit = int(m.group(1)), m.group(2)
    if unit == "d":
        return f"{n} dernier jour" if n == 1 else f"{n} derniers jours"
    if unit == "w":
        return f"{n} dernière semaine" if n == 1 else f"{n} dernières semaines"
    if unit == "y":
        return f"{n} dernière année" if n == 1 else f"{n} dernières années"
    return f"{n} dernier mois" if n == 1 else f"{n} derniers mois"


def _key(series: pd.Series) -> pd.Series:
    """String form for equality tests; 1.0 and 1 compare equal."""
    if pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_bool_dtype(series):
        out = series.astype(str)
        whole = series.notna() & (series % 1 == 0)
        out[whole] = series[whole].astype("int64").astype(str)
        return out
    return series.astype(str)


def _vkey(value: Any) -> str:
    try:
        number = float(value)
        if not isinstance(value, bool) and number.is_integer():
            return str(int(number))
    except (TypeError, ValueError):
        pass
    return str(value)


def _is_all(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip().lower() in _ALL_STRINGS)


def _normalise_multi(value: Any) -> list[Any] | None:
    if _is_all(value):
        return None
    if isinstance(value, (list, tuple, set)):
        items = [v for v in value if not _is_all(v)]
    else:
        items = [v.strip() for v in str(value).split(",") if v.strip()]
    return items or None


def _to_ts(value: Any) -> pd.Timestamp | None:
    if _is_all(value):
        return None
    ts = pd.to_datetime(value, errors="coerce")
    if pd.isna(ts):
        return None
    ts = pd.Timestamp(ts)
    return ts.tz_localize(None) if ts.tzinfo else ts


def resolve_filters(
    specs: list[FilterSpec],
    requested: dict[str, Any],
    mats: dict[str, MatSource],
) -> dict[str, ActiveFilter]:
    """Resolve every filter against the request and the materialised sources."""
    out: dict[str, ActiveFilter] = {}
    for spec in specs:
        raw = requested[spec.id] if spec.id in requested else spec.default
        having = {
            name
            for name, m in mats.items()
            if spec.field in m.df.columns and (spec.applies_to is None or name in spec.applies_to)
        }
        if spec.type == "select":
            value = None if _is_all(raw) else raw
            label = all_label(spec.label) if value is None else str(value)
            out[spec.id] = ActiveFilter(spec, ResolvedFilter(value=value, label=label), value, sources=having)
        elif spec.type == "multi":
            values = _normalise_multi(raw)
            label = all_label(spec.label) if values is None else ", ".join(str(v) for v in values)
            out[spec.id] = ActiveFilter(spec, ResolvedFilter(value=values, label=label), values, sources=having)
        elif spec.type == "relative_period":
            out[spec.id] = _resolve_relative(spec, raw, having, mats)
        elif spec.type in RESHAPING:
            out[spec.id] = _resolve_choice(spec, raw, having)
        else:
            out[spec.id] = _resolve_range(spec, raw, having)
    return out


def _resolve_relative(
    spec: FilterSpec, raw: Any, having: set[str], mats: dict[str, MatSource]
) -> ActiveFilter:
    preset = "all" if _is_all(raw) else str(raw).strip().lower()
    match = _PRESET_RE.match(preset)
    if preset != "all" and preset not in _TO_DATE and not match:
        raise SpecError(
            f"Filtre « {spec.id} » : période « {raw} » inconnue "
            "(attendu <n>d, <n>w, <n>m, <n>y, ytd, mtd ou all ; ex. 7d, 13w, 12m, 1y)."
        )
    lasts = [d for d in (mats[n].real_last_date(spec.field) for n in having) if d is not None]
    if not lasts:
        return ActiveFilter(spec, ResolvedFilter(value=preset, label=period_label(preset)), preset, sources=having)
    last = max(lasts)
    lo: pd.Timestamp | None = None
    if match:
        n, unit = int(match.group(1)), match.group(2)
        lo = last - {
            "d": pd.Timedelta(days=n),
            "w": pd.Timedelta(weeks=n),
            "m": pd.DateOffset(months=n),
            "y": pd.DateOffset(years=n),
        }[unit]
    elif preset in _TO_DATE:
        start = pd.Timestamp(last.year, 1 if preset == "ytd" else last.month, 1)
        lo = start - pd.Timedelta(microseconds=1)  # lower bound is exclusive
    firsts = []
    for name in having:
        dates = pd.to_datetime(mats[name].df[spec.field], errors="coerce")
        window = dates[(dates <= last) & (dates > lo)] if lo is not None else dates[dates <= last]
        if window.notna().any():
            firsts.append(window.min())
    public = ResolvedFilter(
        value=preset,
        label=period_label(preset),
        date_from=min(firsts).strftime("%Y-%m-%d") if firsts else None,
        date_to=last.strftime("%Y-%m-%d"),
    )
    # Only the lower bound cuts: forecast rows after the last real date are kept.
    return ActiveFilter(spec, public, preset, lo_excl=lo, last=last, sources=having)


def _resolve_choice(spec: FilterSpec, raw: Any, having: set[str]) -> ActiveFilter:
    """``time_grain`` / ``aggregation``: a whitelisted keyword, ``None`` = default."""
    allowed, labels, default = (
        (TIME_GRAINS, GRAIN_LABELS, "Détail")
        if spec.type == "time_grain"
        else (AGGREGATIONS, AGGREGATION_LABELS, AGGREGATION_LABELS["sum"])
    )
    value = None if _is_all(raw) or str(raw).strip().lower() == "none" else str(raw).strip().lower()
    if value is not None and value not in allowed:
        raise SpecError(f"Filtre « {spec.id} » : valeur « {raw} » inconnue (attendu {', '.join(allowed)}).")
    label = default if value is None else labels[value]
    return ActiveFilter(spec, ResolvedFilter(value=value, label=label), value, sources=having)


def _resolve_range(spec: FilterSpec, raw: Any, having: set[str]) -> ActiveFilter:
    start: Any = None
    end: Any = None
    if isinstance(raw, dict):
        start, end = raw.get("from"), raw.get("to")
    elif isinstance(raw, (list, tuple)) and len(raw) == 2:
        start, end = raw
    lo, hi = _to_ts(start), _to_ts(end)
    lo_excl = lo - pd.Timedelta(days=1) if lo is not None else None
    hi_incl = hi + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1) if hi is not None else None
    if lo is not None and hi is not None:
        label = f"du {format_date_fr(lo)} au {format_date_fr(hi)}"
    elif lo is not None:
        label = f"depuis le {format_date_fr(lo)}"
    elif hi is not None:
        label = f"jusqu'au {format_date_fr(hi)}"
    else:
        label = "Toute la période"
    value = None if lo is None and hi is None else {"from": start, "to": end}
    public = ResolvedFilter(
        value=value,
        label=label,
        date_from=lo.strftime("%Y-%m-%d") if lo is not None else None,
        date_to=hi.strftime("%Y-%m-%d") if hi is not None else None,
    )
    return ActiveFilter(spec, public, value, lo_excl=lo_excl, hi_incl=hi_incl, sources=having)


def filter_options(specs: list[FilterSpec], mats: dict[str, MatSource]) -> dict[str, list[Any]]:
    """Distinct values of every select / multi filter field (union over sources)."""
    options: dict[str, list[Any]] = {}
    for spec in specs:
        if spec.type not in ("select", "multi"):
            continue
        values: set[Any] = set()
        for name, m in mats.items():
            if spec.field in m.df.columns and (spec.applies_to is None or name in spec.applies_to):
                values.update(m.df[spec.field].dropna().unique().tolist())
        try:
            options[spec.id] = sorted(values)
        except TypeError:
            options[spec.id] = sorted(values, key=str)
    return options


def apply_filters(
    mat: MatSource,
    active: dict[str, ActiveFilter],
    *,
    skip: set[str] | frozenset[str] = frozenset(),
    skip_periods: bool = False,
) -> tuple[pd.DataFrame, dict[str, Any], list[str]]:
    """Filter a source frame.

    Returns ``(frame, applied, ignored)``: ``applied`` maps the ids of the
    filters that restricted this frame to their value; ``ignored`` lists the
    ``skip`` filters that would have applied to it. ``skip_periods`` drops the
    period filters (used to compare with the previous period).
    """
    df = mat.df
    mask: pd.Series | None = None
    applied: dict[str, Any] = {}
    ignored: list[str] = []
    for fid, flt in active.items():
        if mat.name not in flt.sources:
            continue
        if fid in skip:
            ignored.append(fid)
            continue
        if not flt.restricts or (flt.is_period and skip_periods):
            continue
        col = df[flt.spec.field]
        if flt.is_period:
            dates = pd.to_datetime(col, errors="coerce")
            part = pd.Series(True, index=df.index)
            if flt.lo_excl is not None:
                part &= dates > flt.lo_excl
            if flt.hi_incl is not None:
                part &= dates <= flt.hi_incl
            applied[fid] = flt.public.value
        elif flt.spec.type == "multi":
            part = _key(col).isin({_vkey(v) for v in flt.value})
            applied[fid] = flt.value
        else:
            part = _key(col) == _vkey(flt.value)
            applied[fid] = flt.value
        mask = part if mask is None else (mask & part)
    return (df if mask is None else df[mask]), applied, ignored

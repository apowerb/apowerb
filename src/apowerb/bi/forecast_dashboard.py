"""Tables behind the forecast dashboard, built from one th2forecast response.

The dashboard charts read CSV datasets row by row: the UI does not aggregate
numeric columns, so every table here is already shaped for its chart (one row
per series, or one row per forecast period). Pure functions, no I/O.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from apowerb.tools_store.portfolio.api_call import _same_period_last_year, _summarize_series

# Beyond this many series a stacked bar or a donut is unreadable: the smallest
# series are folded into one "Autres" column.
MAX_DISPLAYED_SERIES = 8
OTHERS_LABEL = "Autres"
SINGLE_SERIES_LABEL = "Total"

RELIABILITY_LABELS = {
    "good": "Fiable",
    "fair": "Prudence",
    "poor": "Peu fiable",
}


def _label(series: dict) -> str:
    group = series.get("group")
    return SINGLE_SERIES_LABEL if group in (None, "") else str(group)


def _values(points: list[dict]) -> list[float]:
    return [p["value"] for p in points if isinstance(p.get("value"), (int, float))]


def _relative_uncertainty(points: list[dict]) -> float | None:
    """Mean width of the 80 % band as a share of the forecast, in percent:
    comparable across series of different sizes, unlike the raw width."""
    widths = []
    for p in points:
        lo, hi, value = p.get("lower_80"), p.get("upper_80"), p.get("value")
        if all(isinstance(x, (int, float)) for x in (lo, hi, value)) and value:
            widths.append((hi - lo) / abs(value) * 100)
    return round(sum(widths) / len(widths), 1) if widths else None


def _leaf_series(response: dict) -> list[dict]:
    """Series that carry a forecast. With a hierarchy th2forecast also returns
    aggregate levels; the dashboard shows the grouped series only."""
    series = [s for s in response.get("series") or [] if s.get("forecast")]
    leaves = [s for s in series if not s.get("level")]
    return leaves or series


def series_table(response: dict) -> list[dict[str, Any]]:
    """One row per series: totals, same period last year, reliability, risk."""
    rows = []
    for s in _leaf_series(response):
        forecast = s.get("forecast") or []
        values = _values(forecast)
        if not values:
            continue
        summary = _summarize_series(s)["summary"]
        last_year_mean = _same_period_last_year(s, values)
        total = sum(values)
        peak = max(forecast, key=lambda p: p.get("value") if isinstance(p.get("value"), (int, float)) else float("-inf"))
        rows.append({
            "serie": _label(s),
            "prevu_total": round(total, 2),
            "meme_periode_n1": round(last_year_mean * len(values), 2) if last_year_mean is not None else None,
            "ecart_n1_pct": summary["change_pct"] if last_year_mean is not None else None,
            "tendance": summary["trend"],
            "fiabilite": RELIABILITY_LABELS.get(s.get("reliability"), "Inconnue"),
            "bat_la_prevision_naive": {True: "oui", False: "non"}.get(s.get("beats_baseline"), "inconnu"),
            "modele": s.get("model") or "",
            "prevu_min": round(min(values), 2),
            "prevu_max": round(max(values), 2),
            "pic_prevu": str(peak.get("date") or "")[:10],
            "incertitude_80_pct": _relative_uncertainty(forecast),
        })
    rows.sort(key=lambda r: r["prevu_total"], reverse=True)
    return rows


def displayed_series(table: list[dict]) -> list[str]:
    """Series kept as their own column/slice, largest first; the rest is folded."""
    names = [r["serie"] for r in table]
    if len(names) <= MAX_DISPLAYED_SERIES:
        return names
    return names[: MAX_DISPLAYED_SERIES - 1]


def share_table(table: list[dict]) -> list[dict[str, Any]]:
    """Share of the forecast total per series, smallest ones folded."""
    kept = set(displayed_series(table))
    rows = [{"serie": r["serie"], "prevu_total": r["prevu_total"]} for r in table if r["serie"] in kept]
    rest = sum(r["prevu_total"] for r in table if r["serie"] not in kept)
    if rest:
        rows.append({"serie": OTHERS_LABEL, "prevu_total": round(rest, 2)})
    return rows


def period_table(response: dict, table: list[dict]) -> list[dict[str, Any]]:
    """One row per forecast period, one column per displayed series (wide
    format: a stacked bar reads each numeric column as a series)."""
    kept = displayed_series(table)
    columns = kept + ([OTHERS_LABEL] if len(table) > len(kept) else [])
    by_date: dict[str, dict[str, float]] = {}
    for s in _leaf_series(response):
        name = _label(s)
        column = name if name in kept else OTHERS_LABEL
        for p in s.get("forecast") or []:
            if not isinstance(p.get("value"), (int, float)):
                continue
            day = str(p.get("date") or "")[:10]
            cell = by_date.setdefault(day, dict.fromkeys(columns, 0.0))
            cell[column] = cell.get(column, 0.0) + p["value"]
    return [
        {"periode": day, **{c: round(v, 2) for c, v in cells.items()}}
        for day, cells in sorted(by_date.items())
    ]


def format_period(day: str, frequency: str | None) -> str:
    """A forecast period as a reader says it ("Semaine du 12/01/2026"),
    from its ISO start date; anything else is returned unchanged."""
    try:
        start = date.fromisoformat(str(day)[:10])
    except ValueError:
        return str(day)
    if frequency == "week":
        return f"Semaine du {start:%d/%m/%Y}"
    if frequency == "month":
        return f"{start:%m/%Y}"
    if frequency == "quarter":
        return f"T{(start.month - 1) // 3 + 1} {start.year}"
    if frequency == "year":
        return str(start.year)
    return f"{start:%d/%m/%Y}"


def headline(table: list[dict], periods: list[dict]) -> dict[str, Any]:
    """The four numbers at the top of the dashboard."""
    total = sum(r["prevu_total"] for r in table)
    with_last_year = [r for r in table if r["meme_periode_n1"]]
    change_pct = None
    if with_last_year:
        current = sum(r["prevu_total"] for r in with_last_year)
        previous = sum(r["meme_periode_n1"] for r in with_last_year)
        change_pct = round((current - previous) / previous * 100, 1) if previous else None
    peak_period, peak_value = None, None
    for row in periods:
        value = sum(v for k, v in row.items() if k != "periode")
        if peak_value is None or value > peak_value:
            peak_period, peak_value = row["periode"], value
    return {
        "total": round(total, 2),
        "change_pct": change_pct,
        "reliable": sum(1 for r in table if r["fiabilite"] == RELIABILITY_LABELS["good"]),
        "series": len(table),
        "peak_period": peak_period,
        "peak_value": round(peak_value, 2) if peak_value is not None else None,
    }

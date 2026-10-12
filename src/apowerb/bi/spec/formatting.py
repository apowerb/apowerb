"""
bi/spec/formatting.py
---------------------
fr-FR number formatting for the spec v2 measures (``MeasureSpec.format``).

Thousands are separated by a narrow no-break space (U+202F), the decimal mark
is a comma, and units are separated from the number by a plain space:
``1 234``, ``15,3 k``, ``96,9 %``, ``1,0 sem.``.
"""

from __future__ import annotations

import math
from typing import Any

THIN_SPACE = " "
MISSING = "—"

FORMATS = ("number", "integer", "compact", "percent", "decimal1", "decimal2", "weeks")


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        return math.isnan(float(value)) or math.isinf(float(value))
    except (TypeError, ValueError):
        return True


def _group(int_part: str) -> str:
    """Insert the thousands separator in a string of digits."""
    out: list[str] = []
    for i, ch in enumerate(reversed(int_part)):
        if i and i % 3 == 0:
            out.append(THIN_SPACE)
        out.append(ch)
    return "".join(reversed(out))


def _fixed(value: float, decimals: int, *, trim: bool = False) -> str:
    """``value`` with ``decimals`` decimals, French separators."""
    text = f"{abs(value):.{decimals}f}"
    int_part, _, frac = text.partition(".")
    if trim:
        frac = frac.rstrip("0")
    body = _group(int_part) + ("," + frac if frac else "")
    # "-0" / "-0,0" read badly: a value that rounds to zero has no sign.
    negative = value < 0 and any(c not in "0,.  " for c in body)
    return ("-" if negative else "") + body


def format_value(value: Any, fmt: str = "number", unit: str | None = None) -> str:
    """Format ``value`` according to a ``MeasureSpec.format`` name."""
    if _is_missing(value):
        return MISSING
    v = float(value)
    if fmt == "integer":
        text = _fixed(round(v), 0)
    elif fmt == "compact":
        a = abs(v)
        if a >= 1e9:
            text = _fixed(v / 1e9, 1) + " Md"
        elif a >= 1e6:
            text = _fixed(v / 1e6, 1) + " M"
        elif a >= 1e3:
            text = _fixed(v / 1e3, 1) + " k"
        else:
            text = _fixed(v, 1, trim=True)
    elif fmt == "percent":
        text = _fixed(v * 100, 1) + " %"
    elif fmt == "decimal1":
        text = _fixed(v, 1)
    elif fmt == "decimal2":
        text = _fixed(v, 2)
    elif fmt == "weeks":
        text = _fixed(v, 1) + " sem."
    else:  # number
        text = _fixed(v, 0) if abs(v - round(v)) < 1e-9 else _fixed(v, 2, trim=True)
    if unit and fmt not in ("percent", "weeks"):
        text = f"{text} {unit}"
    return text


def format_delta(current: Any, previous: Any, fmt: str = "number") -> tuple[float | None, str | None]:
    """Delta between two values: ``(delta, "+3,2 %")``.

    A ``percent`` measure is compared in points (``+1,2 pt``); any other
    measure as a relative change. ``(None, None)`` when it cannot be computed.
    """
    if _is_missing(current) or _is_missing(previous):
        return None, None
    cur, prev = float(current), float(previous)
    if fmt == "percent":
        delta = (cur - prev) * 100
        return delta, f"{'+' if delta > 0 else ''}{_fixed(delta, 1)} pt"
    if prev == 0:
        return None, None
    delta = (cur - prev) / abs(prev)
    return delta, f"{'+' if delta > 0 else ''}{_fixed(delta * 100, 1)} %"


_MONTHS_FR = (
    "janv.", "févr.", "mars", "avr.", "mai", "juin",
    "juil.", "août", "sept.", "oct.", "nov.", "déc.",
)


def format_date_fr(value: Any) -> str:
    """``29 déc. 2025`` for a date-like value, ``""`` when missing."""
    try:
        import pandas as pd

        ts = pd.Timestamp(value)
        if pd.isna(ts):
            return ""
        return f"{ts.day} {_MONTHS_FR[ts.month - 1]} {ts.year}"
    except (TypeError, ValueError):
        return ""

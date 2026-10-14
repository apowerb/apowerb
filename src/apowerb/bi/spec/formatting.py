"""
bi/spec/formatting.py
---------------------
Number and date formatting for the spec v2 measures (``MeasureSpec.format``), in the
dashboard language.

French (the default): thousands separated by a narrow no-break space (U+202F), a
decimal comma, units after a plain space: ``1 234``, ``15,3 k``, ``96,9 %``, ``1,0 sem.``.
English: ``1,234``, ``15.3k``, ``96.9%``, ``1.0 wk``.
Plurals: ``format_plural`` (``0 famille``, ``2 familles``; ``1 family``, ``2 families``).
"""

from __future__ import annotations

import math
from typing import Any

THIN_SPACE = " "
MISSING = "—"

FORMATS = ("number", "integer", "compact", "percent", "decimal1", "decimal2", "weeks")
PLURAL = "plural"  # {{measure|plural:one:other}}: the count followed by the word that agrees with it


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        return math.isnan(float(value)) or math.isinf(float(value))
    except (TypeError, ValueError):
        return True


# Separators and suffixes per language: thousands, decimal mark, compact suffixes
# (thousand, million, billion), percent and weeks suffixes.
_STYLE: dict[str, dict[str, Any]] = {
    "fr": {"group": THIN_SPACE, "decimal": ",", "compact": (" k", " M", " Md"), "percent": " %", "weeks": " sem."},
    "en": {"group": ",", "decimal": ".", "compact": ("k", "M", "B"), "percent": "%", "weeks": " wk"},
}


def _style(lang: str | None) -> dict[str, Any]:
    return _STYLE.get(lang or "fr", _STYLE["fr"])


def _group(int_part: str, sep: str = THIN_SPACE) -> str:
    """Insert the thousands separator in a string of digits."""
    out: list[str] = []
    for i, ch in enumerate(reversed(int_part)):
        if i and i % 3 == 0:
            out.append(sep)
        out.append(ch)
    return "".join(reversed(out))


def _fixed(value: float, decimals: int, *, trim: bool = False, lang: str | None = None) -> str:
    """``value`` with ``decimals`` decimals, with the separators of ``lang``."""
    style = _style(lang)
    text = f"{abs(value):.{decimals}f}"
    int_part, _, frac = text.partition(".")
    if trim:
        frac = frac.rstrip("0")
    body = _group(int_part, style["group"]) + (style["decimal"] + frac if frac else "")
    # "-0" / "-0,0" read badly: a value that rounds to zero has no sign.
    negative = value < 0 and any(c.isdigit() and c != "0" for c in body)
    return ("-" if negative else "") + body


def format_value(value: Any, fmt: str = "number", unit: str | None = None, lang: str | None = None) -> str:
    """Format ``value`` according to a ``MeasureSpec.format`` name, in ``lang`` (French by default)."""
    if _is_missing(value):
        return MISSING
    style = _style(lang)
    v = float(value)
    if fmt == "integer":
        text = _fixed(round(v), 0, lang=lang)
    elif fmt == "compact":
        a = abs(v)
        thousand, million, billion = style["compact"]
        if a >= 1e9:
            text = _fixed(v / 1e9, 1, lang=lang) + billion
        elif a >= 1e6:
            text = _fixed(v / 1e6, 1, lang=lang) + million
        elif a >= 1e3:
            text = _fixed(v / 1e3, 1, lang=lang) + thousand
        else:
            text = _fixed(v, 1, trim=True, lang=lang)
    elif fmt == "percent":
        text = _fixed(v * 100, 1, lang=lang) + style["percent"]
    elif fmt == "decimal1":
        text = _fixed(v, 1, lang=lang)
    elif fmt == "decimal2":
        text = _fixed(v, 2, lang=lang)
    elif fmt == "weeks":
        text = _fixed(v, 1, lang=lang) + style["weeks"]
    else:  # number
        text = _fixed(v, 0, lang=lang) if abs(v - round(v)) < 1e-9 else _fixed(v, 2, trim=True, lang=lang)
    if unit and fmt not in ("percent", "weeks"):
        text = f"{text} {unit}"
    return text


def format_plural(value: Any, one: str, other: str, lang: str | None = None) -> str:
    """``value`` then ``one`` or ``other``: French is singular below 2 (``0 famille``), English only for 1."""
    if _is_missing(value):
        return MISSING
    v = float(value)
    singular = abs(v) == 1 if lang == "en" else abs(v) < 2
    return f"{format_value(v, 'number', lang=lang)} {one if singular else other}"


def format_delta(
    current: Any, previous: Any, fmt: str = "number", lang: str | None = None
) -> tuple[float | None, str | None]:
    """Delta between two values: ``(delta, "+3,2 %")``.

    A ``percent`` measure is compared in points (``+1,2 pt``); any other
    measure as a relative change. ``(None, None)`` when it cannot be computed.
    """
    if _is_missing(current) or _is_missing(previous):
        return None, None
    cur, prev = float(current), float(previous)
    if fmt == "percent":
        delta = (cur - prev) * 100
        return delta, f"{'+' if delta > 0 else ''}{_fixed(delta, 1, lang=lang)} pt"
    if prev == 0:
        return None, None
    delta = (cur - prev) / abs(prev)
    return delta, f"{'+' if delta > 0 else ''}{_fixed(delta * 100, 1, lang=lang)}{_style(lang)['percent']}"


_MONTHS = {
    "fr": ("janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."),
    "en": ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
}


def format_date(value: Any, lang: str | None = None) -> str:
    """``29 déc. 2025`` (French) or ``Dec 29, 2025`` (English); ``""`` when missing."""
    try:
        import pandas as pd

        ts = pd.Timestamp(value)
        if pd.isna(ts):
            return ""
        if lang == "en":
            return f"{_MONTHS['en'][ts.month - 1]} {ts.day}, {ts.year}"
        return f"{ts.day} {_MONTHS['fr'][ts.month - 1]} {ts.year}"
    except (TypeError, ValueError):
        return ""


def format_date_fr(value: Any) -> str:
    """``29 déc. 2025`` for a date-like value, ``""`` when missing."""
    return format_date(value, "fr")

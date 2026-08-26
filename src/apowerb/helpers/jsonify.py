"""Recursively coerce values to JSON-safe Python primitives.

NumPy scalars (``numpy.bool_``, ``numpy.int64``, ``numpy.float64`` …) and
arrays sneak out of pandas ``to_dict(orient="records")`` calls because
``astype(object)`` wraps numpy scalars in an object dtype instead of
converting them. Downstream, pydantic's default serializer (used by Google
ADK when persisting events) rejects them with::

    PydanticSerializationError: Unable to serialize unknown type: <class 'numpy.bool'>

and the whole SSE stream is torn down mid-response — from the UI it looks
like the agent silently ignored the file it just read.

A second, unrelated family of values breaks the same write: strings
carrying a NUL character (``U+0000``). JSON and Python are perfectly happy
with them, but PostgreSQL is not — it stores text as UTF-8 and rejects the
NUL byte outright, so persisting the event raises::

    UntranslatableCharacterError: unsupported Unicode escape sequence
    DETAIL:  \u0000 cannot be converted to text.

The tool call is lost and the caller sees an opaque 500. NULs reach a tool
response whenever a tool decodes binary data as text — see the binary guard
in ``tool_read_file``, which is where they came from in practice.

``to_jsonable`` walks a value and converts anything non-JSON into its
closest native Python equivalent, and strips NULs from every string it
meets. It is cheap (no numpy import unless a numpy value is actually
encountered) and safe to apply at tool boundaries.
"""

from __future__ import annotations

from logging import getLogger
from typing import Any

logger = getLogger(__name__)


def strip_nul(text: str) -> str:
    """Remove NUL characters, which PostgreSQL refuses to store in ``text``
    and ``jsonb`` columns. Every other character is left untouched: control
    characters, newlines and tabs are all valid UTF-8 that PostgreSQL
    accepts, and dropping them would silently mangle real content.

    Removals are logged. This runs at every tool boundary, so it is the one
    place a NUL-producing bug in any tool would otherwise disappear without
    trace: the write would start succeeding and nothing would report that a
    tool is emitting corrupted output.
    """
    if "\x00" not in text:
        return text
    logger.warning(
        "stripped %d NUL character(s) from a value before persistence",
        text.count("\x00"),
    )
    return text.replace("\x00", "")


def to_jsonable(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return value

    try:
        import numpy as np
    except ImportError:
        np = None

    # Numpy scalars BEFORE Python primitives because ``numpy.float64`` inherits
    # from ``float`` and ``numpy.bool_`` is its own type: without this order a
    # NaN coming out of pandas would slip through as a raw float and blow up
    # pydantic / JSON later.
    if np is not None:
        if isinstance(value, np.bool_):
            return bool(value)
        if isinstance(value, np.integer):
            return int(value)
        if isinstance(value, np.floating):
            f = float(value)
            return f if f == f else None  # NaN → None
        if isinstance(value, np.ndarray):
            return [to_jsonable(v) for v in value.tolist()]
        if isinstance(value, np.generic):
            return to_jsonable(value.item())

    if isinstance(value, float):
        return value if value == value else None  # NaN → None
    if isinstance(value, str):
        return strip_nul(value)
    if isinstance(value, int):
        return value

    if isinstance(value, dict):
        # Keys are cleaned as well: PostgreSQL rejects a NUL wherever it sits
        # in the document, not only in values.
        cleaned: dict[str, Any] = {}
        for k, v in value.items():
            key = strip_nul(str(k))
            if key in cleaned:
                # Two keys differing only by a NUL collapse into one. Keeping
                # the last is what a dict comprehension would do silently;
                # what matters is that the dropped value is reported, since
                # this module exists to stop losses from going unnoticed.
                logger.warning(
                    "dropping a value whose key collided with %r after "
                    "removing NUL characters",
                    key,
                )
            cleaned[key] = to_jsonable(v)
        return cleaned

    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_jsonable(v) for v in value]

    try:
        import pandas as pd
    except ImportError:
        pd = None

    if pd is not None:
        if value is pd.NaT:
            return None
        if isinstance(value, pd.Timestamp):
            return None if pd.isna(value) else value.isoformat()

    try:
        import datetime as _dt
        if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
            return value.isoformat()
    except Exception:
        pass

    # Last resort: anything with no JSON equivalent becomes its repr. That
    # discards the value's type and structure, so it is reported -- the
    # callback now routes non-dict tool responses through here too, and a
    # structured object quietly flattened into a string is exactly the kind
    # of loss this module is supposed to make visible.
    logger.warning(
        "coercing a %s to its string form; type and structure are lost",
        type(value).__name__,
    )
    return strip_nul(str(value))

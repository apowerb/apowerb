"""Recursively coerce values to JSON-safe Python primitives.

NumPy scalars (``numpy.bool_``, ``numpy.int64``, ``numpy.float64`` …) and
arrays sneak out of pandas ``to_dict(orient="records")`` calls because
``astype(object)`` wraps numpy scalars in an object dtype instead of
converting them. Downstream, pydantic's default serializer (used by Google
ADK when persisting events) rejects them with::

    PydanticSerializationError: Unable to serialize unknown type: <class 'numpy.bool'>

and the whole SSE stream is torn down mid-response — from the UI it looks
like the agent silently ignored the file it just read.

``to_jsonable`` walks a value and converts anything non-JSON into its
closest native Python equivalent. It is cheap (no numpy import unless a
numpy value is actually encountered) and safe to apply at tool boundaries.
"""

from __future__ import annotations

from typing import Any


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
    if isinstance(value, (int, str)):
        return value

    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}

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

    return str(value)

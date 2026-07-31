"""Regression tests for ``helpers.jsonify.to_jsonable``.

Guards the SSE-stream crash seen in production where ADK's pydantic event
serialisation rejected ``numpy.bool`` values coming from a OneDrive Excel
read, tearing down the stream mid-response (users read this as "the agent
rejected my file").
"""

from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd
import pytest

from apowerb.helpers.jsonify import to_jsonable


def test_passes_native_primitives_untouched():
    assert to_jsonable(None) is None
    assert to_jsonable(True) is True
    assert to_jsonable(1) == 1
    assert to_jsonable(1.5) == 1.5
    assert to_jsonable("x") == "x"


def test_converts_numpy_scalars():
    assert to_jsonable(np.bool_(True)) is True
    assert to_jsonable(np.int64(42)) == 42
    assert to_jsonable(np.float32(1.5)) == pytest.approx(1.5)


def test_numpy_nan_becomes_none():
    assert to_jsonable(np.float64("nan")) is None


def test_numpy_array_becomes_list():
    assert to_jsonable(np.array([1, 2, 3])) == [1, 2, 3]


def test_nested_dict_is_recursed():
    payload = {"flag": np.bool_(False), "rows": [{"n": np.int64(7)}]}
    out = to_jsonable(payload)
    assert out == {"flag": False, "rows": [{"n": 7}]}
    # Result must be JSON-serialisable — the whole point of the helper.
    json.dumps(out)


def test_pandas_records_from_dataframe_are_json_safe():
    df = pd.DataFrame({"name": ["a"], "active": [True], "n": [1]})
    records = df.astype(object).where(pd.notna(df), None).to_dict(orient="records")
    # Without to_jsonable, json.dumps would raise on numpy.bool_.
    safe = [to_jsonable(r) for r in records]
    json.dumps(safe)
    assert safe[0]["active"] is True


def test_nat_becomes_none():
    assert to_jsonable(pd.NaT) is None

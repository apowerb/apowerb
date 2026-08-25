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


def test_strips_nul_from_a_string():
    assert to_jsonable("ab\x00cd") == "abcd"


def test_leaves_a_clean_string_byte_for_byte():
    clean = "Ligne 1\nLigne 2\tfin\r\n - accentue (c)"
    assert to_jsonable(clean) == clean


def test_keeps_control_characters_other_than_nul():
    # PostgreSQL accepts every control character except NUL; dropping them
    # would mangle content the caller still needs.
    assert to_jsonable("a\x01b\x1fc") == "a\x01b\x1fc"


def test_strips_nul_inside_nested_structures():
    payload = {"content": ["x\x00y", {"deep": "z\x00"}], "ok": "plain"}
    assert to_jsonable(payload) == {
        "content": ["xy", {"deep": "z"}],
        "ok": "plain",
    }


def test_strips_nul_from_a_stringified_fallback_value():
    class Weird:
        def __str__(self):
            return "we\x00ird"

    assert to_jsonable(Weird()) == "weird"


def test_result_carries_no_nul_escape_once_serialised():
    # PostgreSQL rejects the NUL escape in both text and jsonb, so it must be
    # gone from the serialised payload, not merely from the Python repr.
    serialised = json.dumps(to_jsonable({"content": "a\x00b"}))
    assert "\\u0000" not in serialised


def test_strips_nul_from_dictionary_keys():
    # PostgreSQL rejects a NUL wherever it sits in the document, keys included.
    assert to_jsonable({"a\x00b": 1}) == {"ab": 1}


def test_colliding_keys_keep_the_last_value_and_are_reported(caplog):
    """Two keys differing only by a NUL collapse into one.

    Keeping the last is what a plain comprehension does; the point is that the
    dropped value is reported rather than vanishing.
    """
    with caplog.at_level("WARNING"):
        result = to_jsonable({"a\x00b": 1, "ab": 2})

    assert result == {"ab": 2}
    assert any("collided" in r.message for r in caplog.records)


def test_stripping_a_nul_is_reported(caplog):
    with caplog.at_level("WARNING"):
        to_jsonable({"k": "a\x00b"})

    assert any("stripped" in r.message for r in caplog.records)


def test_clean_values_are_not_reported(caplog):
    with caplog.at_level("WARNING"):
        to_jsonable({"k": "plain", "j": 2})

    assert caplog.records == []

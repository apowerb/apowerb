"""The ADK after_tool_callback must sanitise every response shape.

ADK wraps a non-dict tool result in ``{"result": ...}``, but only after this
callback has run (functions.py, "Specs requires the result to be a dict").
A callback that returned None for non-dicts would therefore let a list or a
string reach the event write untouched -- covering some tools while looking
like it covered all of them.
"""

from __future__ import annotations

import json

from apowerb.core.agent_helpers.agent_utils import sanitize_tool_response


def _sanitise(response):
    return sanitize_tool_response(
        tool=None, args={}, tool_context=None, tool_response=response
    )


def _adk_would_wrap(result):
    """Mirrors ADK: a dict is left alone, anything else is wrapped."""
    return result if isinstance(result, dict) else {"result": result}


def test_dict_response_is_sanitised_in_place():
    assert _sanitise({"status": "success", "content": "a\x00b"}) == {
        "status": "success",
        "content": "ab",
    }


def test_string_response_is_sanitised_and_wrapped():
    assert _sanitise("a\x00b") == {"result": "ab"}


def test_list_response_is_sanitised_and_wrapped():
    assert _sanitise(["a\x00b", {"k": "c\x00d"}]) == {"result": ["ab", {"k": "cd"}]}


def test_wrapping_here_does_not_make_adk_wrap_twice():
    once = _sanitise("a\x00b")
    assert _adk_would_wrap(once) == {"result": "ab"}


def test_clean_dict_is_returned_unchanged():
    clean = {"status": "success", "content": "plain", "line_count": 2}
    assert _sanitise(clean) == clean


def test_no_response_shape_leaves_a_nul_escape_in_the_persisted_json():
    for response in ({"content": "a\x00b"}, "a\x00b", ["a\x00b"], ("a\x00b",)):
        serialised = json.dumps(_adk_would_wrap(_sanitise(response)))
        assert "\\u0000" not in serialised

"""A tool result must survive the trip to the supervision screen.

The screen cut tool results at 200 characters, and the front offered a
"Show more" button whose own collapse threshold was also 200 — so the
button appeared on every result and had nothing left to reveal but the
ellipsis. These tests pin the payload, not the button: as long as the
server hands over the whole result (up to a real bound) the front has
something to expand.
"""
from __future__ import annotations

import json

from apowerb.core.adk_runner import TOOL_RESULT_MAX_CHARS, parse_session_to_trace


def _session_with_tool_result(response_obj):
    return {
        "id": "sess-1",
        "events": [
            {
                "author": "agent1",
                "timestamp": 1.0,
                "content": {
                    "parts": [
                        {
                            "functionResponse": {
                                "name": "send_mail",
                                "response": response_obj,
                            }
                        }
                    ]
                },
            }
        ],
    }


def _tool_result(response_obj):
    trace = parse_session_to_trace(_session_with_tool_result(response_obj), "agent1")
    (step,) = [s for s in trace["steps"] if s["type"] == "tool_result"]
    return step


def test_a_result_longer_than_the_front_threshold_arrives_whole():
    # 200 is the front's collapse threshold. A payload comfortably past it
    # must arrive intact, or "Show more" has nothing to show.
    payload = {"body": "x" * 2_000}
    expected = json.dumps(payload)

    step = _tool_result(payload)

    assert step["content"] == expected
    assert step["details"]["full_length"] == len(expected)
    assert step["details"]["truncated"] is False


def test_the_bound_still_applies_and_says_so():
    # A tool can return megabytes; the bound stays, but it is reported
    # rather than dressed up as content the user could expand into.
    payload = {"body": "x" * (TOOL_RESULT_MAX_CHARS + 5_000)}
    expected = json.dumps(payload)

    step = _tool_result(payload)

    assert len(step["content"]) == TOOL_RESULT_MAX_CHARS
    assert step["content"] == expected[:TOOL_RESULT_MAX_CHARS]
    # No trailing "..." baked into the content: the ellipsis is the front's
    # to draw, and a server-supplied one made a full result look cut.
    assert not step["content"].endswith("...")
    assert step["details"]["truncated"] is True
    assert step["details"]["full_length"] == len(expected)


def test_a_short_result_is_not_flagged_as_cut():
    step = _tool_result({"ok": True})

    assert step["content"] == json.dumps({"ok": True})
    assert step["details"]["truncated"] is False

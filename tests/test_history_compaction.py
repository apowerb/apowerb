"""Tests for ``apowerb.core.history_compaction``."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from apowerb.core.history_compaction import (
    _strip_payload,
    create_strip_large_payloads_callback,
)


# ---------------------------------------------------------------------------
# Pure stripping helper
# ---------------------------------------------------------------------------


def test_strip_payload_replaces_large_data_string():
    big = "A" * 5000
    obj = {"pages": [{"page_number": 1, "data": big, "mime_type": "image/png"}]}

    dropped = _strip_payload(obj)

    assert dropped == 5000
    assert obj["pages"][0]["data"].startswith("<stripped:")
    assert obj["pages"][0]["page_number"] == 1
    assert obj["pages"][0]["mime_type"] == "image/png"


def test_strip_payload_keeps_short_strings():
    obj = {"pages": [{"data": "small"}]}

    dropped = _strip_payload(obj)

    assert dropped == 0
    assert obj["pages"][0]["data"] == "small"


def test_strip_payload_walks_nested_lists():
    big = "B" * 6000
    obj = {
        "status": "success",
        "pages": [
            {"page_number": 1, "data": big},
            {"page_number": 2, "data": big},
        ],
    }

    dropped = _strip_payload(obj)

    assert dropped == 12000
    assert all(p["data"].startswith("<stripped:") for p in obj["pages"])


def test_strip_payload_ignores_non_payload_keys():
    big_url = "C" * 9000  # large string but under a non-payload key
    obj = {"url": big_url, "page_number": 1}

    dropped = _strip_payload(obj)

    assert dropped == 0
    assert obj["url"] == big_url


# ---------------------------------------------------------------------------
# Callback wiring
# ---------------------------------------------------------------------------


def _make_part_with_function_response(response_dict):
    """Build a part-like object exposing ``function_response.response``."""
    fr = SimpleNamespace(response=response_dict)
    return SimpleNamespace(function_response=fr)


def _make_text_part(text):
    return SimpleNamespace(function_response=None, text=text)


def _make_content(parts):
    return SimpleNamespace(parts=parts)


def _make_request(contents):
    return SimpleNamespace(contents=contents)


def test_callback_strips_function_response_in_older_turns():
    big = "X" * 8000
    old_response = {"pages": [{"data": big, "page_number": 1}]}
    latest_response = {"pages": [{"data": "Y" * 7000, "page_number": 1}]}

    request = _make_request(
        [
            _make_content([_make_text_part("user asks for AR")]),
            _make_content([_make_part_with_function_response(old_response)]),
            _make_content([_make_text_part("LLM extracted: CF0916, 2 lines")]),
            _make_content([_make_part_with_function_response(latest_response)]),
        ]
    )

    cb = create_strip_large_payloads_callback("agent6")
    result = cb(callback_context=SimpleNamespace(), llm_request=request)

    assert result is None  # no short-circuit response
    # Older turn was stripped.
    assert old_response["pages"][0]["data"].startswith("<stripped:")
    # Latest turn was preserved verbatim.
    assert latest_response["pages"][0]["data"] == "Y" * 7000


def test_callback_noop_on_short_history():
    """Single-content requests have no 'older turn' to strip."""
    big = "Z" * 9000
    only_response = {"pages": [{"data": big}]}
    request = _make_request(
        [_make_content([_make_part_with_function_response(only_response)])]
    )

    cb = create_strip_large_payloads_callback("agent6")
    cb(callback_context=SimpleNamespace(), llm_request=request)

    # Nothing stripped because it's the latest (and only) turn.
    assert only_response["pages"][0]["data"] == big


def test_callback_handles_empty_contents():
    request = _make_request([])
    cb = create_strip_large_payloads_callback("agent6")

    # Should not raise.
    assert cb(callback_context=SimpleNamespace(), llm_request=request) is None


def test_callback_handles_parts_without_function_response():
    """Text-only history (no tool calls yet) must be a no-op."""
    request = _make_request(
        [
            _make_content([_make_text_part("hello")]),
            _make_content([_make_text_part("how can I help?")]),
        ]
    )
    cb = create_strip_large_payloads_callback("agent6")

    assert cb(callback_context=SimpleNamespace(), llm_request=request) is None


def test_callback_returns_none_so_chain_continues():
    """The strip callback never short-circuits — it only mutates the request."""
    request = _make_request([_make_content([_make_text_part("a")])])
    cb = create_strip_large_payloads_callback("agent6")

    assert cb(callback_context=SimpleNamespace(), llm_request=request) is None


# ---------------------------------------------------------------------------
# Always-attached guarantees (regression: SequentialAgent sub-agents)
# ---------------------------------------------------------------------------


def test_callback_is_noop_for_agent_without_base64_history():
    """Agent without ``tool_pdf_to_images`` (e.g. BI dashboard, RAG) gets the
    callback attached now — it must be a clean no-op so attaching everywhere
    is safe.

    Regression: ``agent_utils.to_agent()`` attaches the strip callback to
    every agent regardless of its tools. This guards downstream sub-agents
    in a ``SequentialAgent`` pipeline whose history inherits base64 blobs
    from an upstream sub-agent that did call ``tool_pdf_to_images``.
    Agents with no base64 anywhere must pay nothing.
    """
    request = _make_request(
        [
            _make_content([_make_text_part("user: show me the chart")]),
            _make_content([_make_text_part("assistant: here is the chart")]),
        ]
    )
    cb = create_strip_large_payloads_callback("bi_dashboard_agent")

    assert cb(callback_context=SimpleNamespace(), llm_request=request) is None
    # No mutation occurred — history is intact.
    assert request.contents[0].parts[0].text == "user: show me the chart"
    assert request.contents[1].parts[0].text == "assistant: here is the chart"


def test_callback_strips_base64_inherited_from_upstream_subagent():
    """Sub-agent matcher (no ``tool_pdf_to_images``) inherits the history of
    sub-agent intake (which called ``tool_pdf_to_images``). Without the
    strip callback on matcher, Gemini would receive the full base64 blobs
    of every PDF page and overflow its context window.

    Regression: this is the bug observed on SCEI agent12 ``SequentialAgent``
    pipeline (intake → matcher → recorder → notifier) where matcher
    crashed with ``ContextWindowExceededError`` because the strip callback
    was previously gated behind a ``tool_pdf_to_images`` presence check.
    """
    big_page = "P" * 9000  # simulates a base64 PNG page from pdf_to_images
    intake_response = {
        "status": "success",
        "pages": [
            {"page_number": 1, "mime_type": "image/png", "data": big_page},
            {"page_number": 2, "mime_type": "image/png", "data": big_page},
        ],
    }
    latest = {"status": "pending"}
    request = _make_request(
        [
            _make_content([_make_text_part("matcher prompt")]),
            _make_content([_make_part_with_function_response(intake_response)]),
            _make_content([_make_part_with_function_response(latest)]),
        ]
    )

    # ``agent_matcher`` has no pdf_to_images tool — but it still gets the
    # callback attached, and the callback must strip the inherited blobs.
    cb = create_strip_large_payloads_callback("agent_matcher")
    result = cb(callback_context=SimpleNamespace(), llm_request=request)

    assert result is None
    # The two base64 pages from the upstream intake turn were stripped.
    assert intake_response["pages"][0]["data"].startswith("<stripped:")
    assert intake_response["pages"][1]["data"].startswith("<stripped:")
    # Latest turn left untouched.
    assert latest["status"] == "pending"

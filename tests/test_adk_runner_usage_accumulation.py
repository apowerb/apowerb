"""Tests for token usage accumulation in parse_session_to_trace.

The 'usage_metadata' dict on events uses snake_case keys (confirmed by
reading the existing prompt_token_count / candidates_token_count
accumulation in apowerb.core.adk_runner) — unlike part-level keys such
as 'functionCall' / 'functionResponse', which are camelCase.
"""
from __future__ import annotations

from apowerb.core.adk_runner import parse_session_to_trace


def _event(usage_metadata=None, author="agent1", timestamp=1.0):
    return {
        "author": author,
        "timestamp": timestamp,
        "content": None,
        "usage_metadata": usage_metadata,
    }


def test_accumulates_thoughts_and_cached_tokens_snake_case():
    session_data = {
        "id": "sess-1",
        "events": [
            _event(
                usage_metadata={
                    "prompt_token_count": 100,
                    "candidates_token_count": 40,
                    "thoughts_token_count": 15,
                    "cached_content_token_count": 5,
                    "total_token_count": 160,
                }
            ),
            _event(
                usage_metadata={
                    "prompt_token_count": 20,
                    "candidates_token_count": 10,
                    "thoughts_token_count": 3,
                    "cached_content_token_count": 2,
                    "total_token_count": 35,
                }
            ),
        ],
    }

    result = parse_session_to_trace(session_data, "agent1")
    summary = result["summary"]

    assert summary["total_tokens"] == (100 + 40) + (20 + 10)
    assert summary["thoughts_tokens"] == 15 + 3
    assert summary["cached_tokens"] == 5 + 2


def test_missing_usage_metadata_defaults_new_fields_to_zero():
    session_data = {"id": "sess-2", "events": [_event(usage_metadata=None)]}

    result = parse_session_to_trace(session_data, "agent1")
    summary = result["summary"]

    assert summary["thoughts_tokens"] == 0
    assert summary["cached_tokens"] == 0


def test_partial_thoughts_or_cached_absent_treated_as_zero():
    session_data = {
        "id": "sess-3",
        "events": [
            _event(usage_metadata={"prompt_token_count": 5, "candidates_token_count": 2}),
        ],
    }

    result = parse_session_to_trace(session_data, "agent1")
    summary = result["summary"]

    assert summary["thoughts_tokens"] == 0
    assert summary["cached_tokens"] == 0
    assert summary["total_tokens"] == 7

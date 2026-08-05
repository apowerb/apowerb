"""agent_id reaches a filesystem path unvalidated, in four places.

`_validate_upload_id` was added for exactly this reason on `upload_id`, but
`agent_id` was left out — CodeQL flags it as py/path-injection (high) on
`files.py`. `_validate_agent_ownership()` checks that the caller owns the
agent; it says nothing about the *shape* of the string, so an id containing
".." passes ownership and still walks out of uploads_dir().

Same class of bug as the artifacts router on 2026-08-04: `os.path.basename("..")`
returns ".." unchanged, so stripping directory components is not enough — the
traversal values have to be rejected outright.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from apowerb.routers.files import _safe_agent_id


@pytest.mark.parametrize(
    "value",
    ["agent12", "agent1164", "12", "a-b_c", "A1"],
)
def test_legitimate_agent_ids_pass(value):
    _safe_agent_id(value)


@pytest.mark.parametrize(
    "value",
    [
        "..",
        ".",
        "../../etc",
        "agent12/../../etc",
        "/etc/passwd",
        "agent12/sub",
        "",
        "a" * 200,
        "agent 12",
        "agent;rm -rf /",
    ],
)
def test_traversal_and_junk_are_rejected(value):
    with pytest.raises(HTTPException) as exc:
        _safe_agent_id(value)
    assert exc.value.status_code == 400


def test_the_message_names_the_field():
    """A 400 that says "invalid" without saying what is unusable in support."""
    with pytest.raises(HTTPException) as exc:
        _safe_agent_id("../..")
    assert "agent_id" in exc.value.detail


def test_it_returns_the_checked_value_not_just_raises():
    """The guard must hand back the value callers then use.

    A guard that only raises leaves the raw parameter in scope, and the
    tainted value is what reaches the path expression -- CodeQL keeps
    reporting py/path-injection, correctly. Returning forces callers to use
    the checked value.
    """
    assert _safe_agent_id("agent12") == "agent12"

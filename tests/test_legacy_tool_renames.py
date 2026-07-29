"""Tests for the legacy tool-name resolver in ``load_agent_tools_functions``.

Background: ``onedrive_read.tool_read_file`` and ``google_drive.tool_read_file``
were renamed to ``tool_read_onedrive_file`` and ``tool_read_drive_file`` to
eliminate the Python-name collision with ``basic.tool_read_file`` (the
collision used to crash agents with Vertex AI 400 INVALID_ARGUMENT).

Existing agents stored in DB may still reference the old names in their
``agent_tools`` JSON. The resolver translates the old names transparently
and logs a WARNING so operations can spot un-migrated agents.
"""
from __future__ import annotations

import logging

import pytest

from th2agent.tools_store.tools_helpers import (
    _LEGACY_TOOL_RENAMES,
    load_agent_tools_functions,
)


def test_legacy_map_contains_renamed_tools():
    assert (
        _LEGACY_TOOL_RENAMES["onedrive_read.tool_read_file"]
        == "onedrive_read.tool_read_onedrive_file"
    )
    assert (
        _LEGACY_TOOL_RENAMES["google_drive.tool_read_file"]
        == "google_drive.tool_read_drive_file"
    )


def test_onedrive_read_tool_resolves_under_new_name():
    """The renamed function must be discoverable in the portfolio module."""
    from th2agent.tools_store.portfolio import onedrive_read

    assert hasattr(onedrive_read, "tool_read_onedrive_file")
    assert not hasattr(onedrive_read, "tool_read_file")


def test_google_drive_tool_resolves_under_new_name():
    from th2agent.tools_store.portfolio import google_drive

    assert hasattr(google_drive, "tool_read_drive_file")
    assert not hasattr(google_drive, "tool_read_file")


def test_legacy_name_is_resolved_with_warning(caplog):
    """An agent referencing the old name still loads the tool, with a WARNING."""
    caplog.set_level(logging.WARNING, logger="th2agent.tools_store.tools_helpers")

    names, funcs = load_agent_tools_functions(
        ["onedrive_read.tool_read_file"],
        owner_id="test@example.com",
    )

    assert names == ["onedrive_read.tool_read_onedrive_file"]
    assert len(funcs) == 1
    assert funcs[0].__name__ == "tool_read_onedrive_file"

    matching = [
        r for r in caplog.records if "Legacy tool name" in r.getMessage()
    ]
    assert len(matching) == 1
    msg = matching[0].getMessage()
    assert "onedrive_read.tool_read_file" in msg
    assert "tool_read_onedrive_file" in msg


def test_legacy_google_drive_resolves(caplog):
    caplog.set_level(logging.WARNING, logger="th2agent.tools_store.tools_helpers")

    names, funcs = load_agent_tools_functions(
        ["google_drive.tool_read_file"],
        owner_id="test@example.com",
    )

    assert names == ["google_drive.tool_read_drive_file"]
    assert len(funcs) == 1
    assert funcs[0].__name__ == "tool_read_drive_file"


def test_basic_tool_read_file_unchanged():
    """``basic.tool_read_file`` keeps its name — only the OneDrive/Drive
    versions were renamed (path-based local read stays canonical)."""
    from th2agent.tools_store.portfolio import basic

    assert hasattr(basic, "tool_read_file")
    names, funcs = load_agent_tools_functions(
        ["basic.tool_read_file"],
        owner_id="test@example.com",
    )
    assert names == ["basic.tool_read_file"]


def test_no_collision_when_both_modules_loaded():
    """Loading basic + onedrive_read together no longer produces two functions
    with the same Python ``__name__`` — the root cause of the Vertex 400."""
    names, funcs = load_agent_tools_functions(
        ["basic.tool_read_file", "onedrive_read.tool_read_onedrive_file"],
        owner_id="test@example.com",
    )
    py_names = [f.__name__ for f in funcs]
    assert py_names == ["tool_read_file", "tool_read_onedrive_file"]
    assert len(set(py_names)) == len(py_names)


def test_unknown_tool_silently_skipped():
    """Unknown tool names are skipped (existing behaviour) — the legacy map
    must not absorb arbitrary unknowns."""
    names, funcs = load_agent_tools_functions(
        ["onedrive_read.tool_does_not_exist"],
        owner_id="test@example.com",
    )
    assert names == []
    assert funcs == []

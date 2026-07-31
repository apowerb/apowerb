"""Tests for ``dedupe_tools_by_name`` — Vertex AI 400 INVALID_ARGUMENT fix.

Background: ``tools_store/portfolio/{basic,onedrive_read,google_drive}`` each
expose a function literally named ``tool_read_file``. When an agent loads
two of them, ADK pushes both into ``function_declarations`` and Gemini
rejects the whole call with ``Duplicate function declaration``.

The dedup keeps the first occurrence (priority = order in agent_tools)
and logs a WARNING with the source module of every dropped tool.
"""
from __future__ import annotations

import logging

from apowerb.core.agent_helpers.tools_binder import dedupe_tools_by_name


def _named(name: str, module: str = "test.module"):
    def fn():
        return None
    fn.__name__ = name
    fn.__module__ = module
    return fn


def test_no_duplicates_returns_same_order():
    a = _named("tool_a", "mod_a")
    b = _named("tool_b", "mod_b")
    c = _named("tool_c", "mod_c")
    out = dedupe_tools_by_name([a, b, c], agent_name="agent1")
    assert out == [a, b, c]


def test_keeps_first_occurrence():
    first = _named("tool_read_file", "apowerb.tools_store.portfolio.basic")
    second = _named("tool_read_file", "apowerb.tools_store.portfolio.onedrive_read")
    out = dedupe_tools_by_name([first, second], agent_name="agent3")
    assert out == [first]


def test_drops_all_subsequent_duplicates():
    first = _named("tool_read_file", "basic")
    dup1 = _named("tool_read_file", "onedrive_read")
    dup2 = _named("tool_read_file", "google_drive")
    other = _named("tool_send_email", "outlook_mail")
    out = dedupe_tools_by_name([first, dup1, other, dup2], agent_name="agentX")
    assert out == [first, other]


def test_logs_warning_with_source_module(caplog):
    caplog.set_level(logging.WARNING, logger="apowerb.core.agent_helpers.tools_binder")
    first = _named("tool_read_file", "apowerb.tools_store.portfolio.basic")
    dup = _named("tool_read_file", "apowerb.tools_store.portfolio.onedrive_read")
    dedupe_tools_by_name([first, dup], agent_name="agent3")
    matching = [r for r in caplog.records if "tool_read_file" in r.getMessage()]
    assert len(matching) == 1
    msg = matching[0].getMessage()
    assert "agent3" in msg
    assert "onedrive_read" in msg
    assert "basic" in msg


def test_empty_list_returns_empty():
    assert dedupe_tools_by_name([], agent_name="empty_agent") == []


def test_tools_without_name_are_kept():
    class Callable:
        def __call__(self):
            return None

    anon1 = Callable()
    anon2 = Callable()
    a = _named("tool_a", "mod_a")
    out = dedupe_tools_by_name([anon1, a, anon2], agent_name="weird_agent")
    assert out == [anon1, a, anon2]


def test_preserves_priority_order_with_mixed_duplicates():
    """Real-world scenario: agent_tools first, MCP/skills next, auto-tools last."""
    onedrive_read_file = _named(
        "tool_read_file", "apowerb.tools_store.portfolio.onedrive_read"
    )
    onedrive_list = _named("tool_list_files", "apowerb.tools_store.portfolio.onedrive_read")
    basic_read_file = _named("tool_read_file", "apowerb.tools_store.portfolio.basic")
    basic_pdf = _named("tool_pdf_to_images", "apowerb.tools_store.portfolio.basic")
    notify = _named("notify_user", "apowerb.core.agent_helpers.agent_utils")

    out = dedupe_tools_by_name(
        [onedrive_read_file, onedrive_list, basic_read_file, basic_pdf, notify],
        agent_name="email_marketing_assistant",
    )
    assert out == [onedrive_read_file, onedrive_list, basic_pdf, notify]
    assert basic_read_file not in out

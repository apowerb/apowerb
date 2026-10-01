"""Acceptance tests for the Python Script tool (issue #151).

Each test maps to an acceptance criterion:

1. Hidden from the store unless enabled (self-hosted + flag on).
2. A direct call in a disabled build returns FEATURE_DISABLED, never runs.
3. Inputs reach the script; stdout/stderr/return_code/duration are captured.
4. The configured timeout terminates a runaway process (whole group).
5. A script exception surfaces as an error dict — the host never crashes.
6. Even when enabled, an agent cannot self-add it from the catalogue.
"""

from __future__ import annotations

import pytest

from apowerb.configs.settings import get_settings
from apowerb.tools_store.portfolio.python_script import tool_run_python_script
from apowerb.tools_store.tool_manager import ToolsStore


@pytest.fixture
def enabled(monkeypatch):
    """Turn the feature on for the duration of a test (reverts after)."""
    settings = get_settings()
    monkeypatch.setattr(settings, "enable_python_script_tool", True, raising=False)
    return settings


@pytest.fixture
def disabled(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "enable_python_script_tool", False, raising=False)
    return settings


# ── AC#1: visibility gate ────────────────────────────────────────────────
def test_category_hidden_when_flag_off(disabled):
    store = ToolsStore()
    assert "python_script" not in store.get_categories()
    assert "python_script" not in store.get_all_tools()


def test_category_visible_when_flag_on(enabled):
    store = ToolsStore()
    assert "python_script" in store.get_categories()
    assert "python_script.tool_run_python_script" in store.get_tools_in_category(
        "python_script"
    )


# ── AC#2: disabled build refuses to run ──────────────────────────────────
def test_direct_call_disabled_returns_feature_disabled(disabled):
    result = tool_run_python_script("print('should not run')")
    assert result["status"] == "error"
    assert result["error_code"] == "FEATURE_DISABLED"
    # It must not have executed anything.
    assert "stdout" not in result


# ── AC#3: inputs + captured outputs ──────────────────────────────────────
def test_captures_stdout_stderr_and_exit(enabled):
    script = (
        "import sys\n" "print('hello world')\n" "sys.stderr.write('a warning\\n')\n"
    )
    result = tool_run_python_script(script)
    assert result["status"] == "success"
    assert "hello world" in result["stdout"]
    assert "a warning" in result["stderr"]
    assert result["return_code"] == 0
    assert result["timed_out"] is False
    assert isinstance(result["duration_s"], float)


def test_inputs_are_exposed_to_the_script(enabled):
    result = tool_run_python_script("print(inputs['x'] * 2)", inputs={"x": 21})
    assert result["status"] == "success"
    assert result["stdout"].strip() == "42"


def test_rejects_non_serialisable_inputs(enabled):
    result = tool_run_python_script("print(1)", inputs={"bad": {1, 2, 3}})
    assert result["status"] == "error"
    assert result["error_code"] == "INVALID_INPUTS"


def test_rejects_empty_script(enabled):
    result = tool_run_python_script("   ")
    assert result["status"] == "error"
    assert result["error_code"] == "INVALID_SCRIPT"


# ── AC#4: timeout terminates a runaway ───────────────────────────────────
def test_timeout_kills_runaway(enabled):
    # Busy loop that would never return on its own.
    result = tool_run_python_script("while True:\n    pass\n", timeout=1)
    assert result["status"] == "error"
    assert result["timed_out"] is True
    assert result["error_code"] == "TIMEOUT"
    # Killed near the budget, not left running for the busy loop's lifetime.
    assert result["duration_s"] < 10


def test_timeout_is_capped_by_settings(enabled, monkeypatch):
    monkeypatch.setattr(enabled, "python_script_max_timeout", 2, raising=False)
    # Requesting a huge timeout must not let a runaway outlive the cap.
    result = tool_run_python_script("while True:\n    pass\n", timeout=9999)
    assert result["timed_out"] is True
    assert result["duration_s"] < 10


# ── AC#5: a script error is a dict, not a raised exception ────────────────
def test_script_exception_returns_error_dict(enabled):
    result = tool_run_python_script("raise ValueError('boom')")
    assert result["status"] == "error"
    assert result["error_code"] == "NON_ZERO_EXIT"
    assert result["return_code"] == 1
    assert "ValueError" in result["stderr"]
    assert result["timed_out"] is False


# ── Network guard ────────────────────────────────────────────────────────
def test_network_blocked_by_default(enabled):
    script = (
        "import socket\n"
        "try:\n"
        "    socket.socket()\n"
        "    print('OPEN')\n"
        "except OSError as e:\n"
        "    print('BLOCKED')\n"
    )
    result = tool_run_python_script(script)
    assert result["status"] == "success"
    assert result["stdout"].strip() == "BLOCKED"


def test_network_allowed_when_configured(enabled, monkeypatch):
    monkeypatch.setattr(enabled, "python_script_allow_network", True, raising=False)
    script = (
        "import socket\n"
        "s = socket.socket()\n"  # construction must not raise
        "s.close()\n"
        "print('OPEN')\n"
    )
    result = tool_run_python_script(script)
    assert result["status"] == "success"
    assert result["stdout"].strip() == "OPEN"


# ── Output is bounded (host-memory DoS guard) ────────────────────────────
def test_large_output_is_truncated(enabled, monkeypatch):
    # Cap output at 1 KiB, then print well beyond it.
    monkeypatch.setattr(enabled, "python_script_max_output_kb", 1, raising=False)
    result = tool_run_python_script("print('x' * 50000)")
    assert result["status"] == "success"
    assert result["output_truncated"] is True
    assert len(result["stdout"]) < 5000  # bounded, not the full 50k
    assert "truncated" in result["stdout"]


# ── AC#6: not self-addable from the agent catalogue ──────────────────────
def test_not_in_agent_catalogue_even_when_enabled(enabled):
    from apowerb.core.agent_helpers import tool_catalog

    tool_catalog.catalog_entries.cache_clear()
    try:
        entries = tool_catalog.catalog_entries()
        assert "python_script.tool_run_python_script" not in entries
        ok, _msg = tool_catalog.addable_tool("python_script.tool_run_python_script")
        assert ok is False
    finally:
        tool_catalog.catalog_entries.cache_clear()

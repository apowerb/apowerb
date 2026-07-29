"""Tests for the ``th2agent agents`` CLI.

Live regression 2026-05-12 on SCEI_PROD: ``agents list`` crashed with
``psycopg2.ProgrammingError: can't adapt type 'AgentStore'`` because
the CLI was passing the *store instance* into ``fetch_agents()`` which
expected a ``user_id`` string. These tests guard against that pattern
coming back, and exercise the new ``--owner`` flag.
"""

from __future__ import annotations

import inspect
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from th2agent.cli.agents import app
from th2agent.core.agent_main import fetch_agents


# ---------------------------------------------------------------------------
# fetch_agents signature contract
# ---------------------------------------------------------------------------


class TestFetchAgentsSignature:
    """``fetch_agents`` is called from API routers (user-scoped) AND from
    the CLI (admin mode, no owner). The signature must accept ``None``
    so the CLI can list everything without smuggling a placeholder
    ``"admin"`` email or, worse, an instance of the store."""

    def test_user_id_is_optional(self):
        sig = inspect.signature(fetch_agents)
        param = sig.parameters.get("user_id")
        assert param is not None, "fetch_agents must accept a user_id parameter"
        assert param.default is None, (
            "fetch_agents(user_id=None) must be the documented admin path; "
            "if you remove the default, the CLI breaks again"
        )

    def test_docstring_warns_about_none(self):
        """The ``None`` mode is dangerous in a multi-tenant context.
        The docstring must call it out so a future router author does
        not pass ``None`` from a request handler."""
        doc = (fetch_agents.__doc__ or "").lower()
        assert "admin" in doc or "cli" in doc, (
            "fetch_agents docstring must flag the None mode as admin/CLI-only"
        )
        assert "tenant" in doc or "owner" in doc, (
            "docstring must mention the cross-tenant leak risk so callers "
            "know not to pass None from a user-facing path"
        )


# ---------------------------------------------------------------------------
# CLI smoke (with fetch_agents mocked — no DB needed)
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_agents():
    return [
        {
            "agent_id": 6,
            "agent_name": "scei_ar_assistant",
            "owner_id": "com@scei88.fr",
            "agent_model": "anthropic/claude-sonnet-4-5-20250929",
            "superagent_template_id": "scei_ar_assistant",
            "agent_description": "SCEI ARs Assistant",
        },
        {
            "agent_id": 7,
            "agent_name": "other_agent",
            "owner_id": "alice@example.com",
            "agent_model": "gpt-4",
            "superagent_template_id": None,
            "agent_description": "Other",
        },
    ]


class TestCliAgentsList:
    def test_list_without_owner_calls_fetch_with_none(self, fake_agents):
        runner = CliRunner()
        with patch("th2agent.cli.agents.fetch_agents", return_value=fake_agents) as mock_fetch, \
             patch("th2agent.cli.agents.get_agent_store"):
            result = runner.invoke(app, ["list"])
        assert result.exit_code == 0, result.output
        # Critical: must call with user_id=None (admin mode), NOT with a
        # placeholder string or — the regression we are guarding —
        # the store instance.
        mock_fetch.assert_called_once_with(user_id=None)
        # Header must say "all owners" so the operator knows what they
        # are looking at.
        assert "all owners" in result.output.lower()

    def test_list_with_owner_filters_to_that_owner(self, fake_agents):
        runner = CliRunner()
        with patch("th2agent.cli.agents.fetch_agents", return_value=fake_agents[:1]) as mock_fetch, \
             patch("th2agent.cli.agents.get_agent_store"):
            result = runner.invoke(app, ["list", "--owner", "com@scei88.fr"])
        assert result.exit_code == 0, result.output
        mock_fetch.assert_called_once_with(user_id="com@scei88.fr")
        assert "com@scei88.fr" in result.output

    def test_list_prints_real_field_names_not_stale_aliases(self, fake_agents):
        """The previous CLI printed ``agent.get('id', 'N/A')`` /
        ``agent.get('name', 'N/A')`` which never matched the dict keys
        ``agent_id`` / ``agent_name`` — every line read ``ID: N/A``.
        Make sure the printed output now shows the real values."""
        runner = CliRunner()
        with patch("th2agent.cli.agents.fetch_agents", return_value=fake_agents), \
             patch("th2agent.cli.agents.get_agent_store"):
            result = runner.invoke(app, ["list"])
        assert result.exit_code == 0
        out = result.output
        # If the aliases regressed, every line would print N/A.
        # Asserting on the real values catches that.
        assert "6" in out and "scei_ar_assistant" in out
        assert "com@scei88.fr" in out
        assert "anthropic/claude-sonnet-4-5-20250929" in out
        # Owner header line must be present (it is the new info this PR adds)
        assert "Owner:" in out

    def test_list_empty_db_does_not_crash(self):
        runner = CliRunner()
        with patch("th2agent.cli.agents.fetch_agents", return_value=[]), \
             patch("th2agent.cli.agents.get_agent_store"):
            result = runner.invoke(app, ["list"])
        assert result.exit_code == 0
        assert "No agents found" in result.output

    def test_list_does_not_pass_agent_store_instance(self, fake_agents):
        """Anti-regression on the 2026-05-12 bug: the CLI must NEVER
        pass the AgentStore instance to fetch_agents. Verified by
        capturing the kwargs and ensuring the value is either None or
        a plain string."""
        runner = CliRunner()
        captured = {}

        def _capture(**kwargs):
            captured.update(kwargs)
            return fake_agents

        with patch("th2agent.cli.agents.fetch_agents", side_effect=_capture), \
             patch("th2agent.cli.agents.get_agent_store"):
            result = runner.invoke(app, ["list"])
        assert result.exit_code == 0
        val = captured.get("user_id", "SENTINEL")
        assert val is None or isinstance(val, str), (
            f"fetch_agents was called with user_id={val!r} — must be a "
            f"string (the owner email) or None (admin mode), never an "
            f"AgentStore instance or any other object"
        )

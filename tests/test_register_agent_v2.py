"""Regression tests for register_agent / update_agent / _TEMPLATE_RESYNC_FIELDS.

Bug: `output_key`, `output_schema_name`, `skip_when_upstream` were
added to `AgentCreateSchema` (PR #174 + #176) and to the
`th2agents_store` table (auto-migration via `ensure_columns`), but
`register_agent.insert().values(...)` and `update_agent.update().values(...)`
were never updated to include them. Result: agents created via
`POST /api/agents` (UI Agent Factory) or via `register_agent()`
directly would have NULL values for these 3 fields, even when the
caller passed them — silently breaking the entire scei_ar_assistant_v2
sub-agent pipeline.

These tests guard against the regression by source-inspecting the two
SQL builders and the resync field tuple. A higher-fidelity SQLAlchemy
in-memory test would require recreating the th2agents_store DDL in
SQLite (PostgreSQL-specific RETURNING + JSON columns), which is more
trouble than it's worth for a one-line-each fix.
"""

from __future__ import annotations

import inspect


class TestRegisterAgentPersistsV2Fields:
    def test_register_agent_writes_output_key(self):
        from th2agent.core import agent_main

        src = inspect.getsource(agent_main.register_agent)
        assert "output_key=" in src, (
            "register_agent INSERT must include `output_key=` in .values(...) "
            "— otherwise downstream sub-agents in scei_ar_assistant_v2 won't "
            "see upstream payload (NULL → ADK skips output_key plumbing)."
        )

    def test_register_agent_writes_output_schema_name(self):
        from th2agent.core import agent_main

        src = inspect.getsource(agent_main.register_agent)
        assert "output_schema_name=" in src, (
            "register_agent INSERT must include `output_schema_name=` so the "
            "after_agent_callback wiring (PR #174) can resolve the Pydantic "
            "schema and validate the agent's JSON output."
        )

    def test_register_agent_writes_skip_when_upstream(self):
        from th2agent.core import agent_main

        src = inspect.getsource(agent_main.register_agent)
        assert "skip_when_upstream=" in src, (
            "register_agent INSERT must include `skip_when_upstream=` so the "
            "skip-cascade callback (PR #176) is wired at runtime."
        )


class TestUpdateAgentPersistsV2Fields:
    def test_update_agent_writes_output_key(self):
        from th2agent.core import agent_main

        src = inspect.getsource(agent_main.update_agent)
        assert "output_key=" in src, (
            "update_agent must persist output_key on subsequent edits "
            "(currently silently drops the field, leaving NULL even after "
            "a UI edit that sets it)."
        )

    def test_update_agent_writes_output_schema_name(self):
        from th2agent.core import agent_main

        src = inspect.getsource(agent_main.update_agent)
        assert "output_schema_name=" in src

    def test_update_agent_writes_skip_when_upstream(self):
        from th2agent.core import agent_main

        src = inspect.getsource(agent_main.update_agent)
        assert "skip_when_upstream=" in src


class TestTemplateResyncFieldsCoverV2Fields:
    def test_resync_includes_output_key(self):
        from th2agent.core.superagents import _TEMPLATE_RESYNC_FIELDS

        assert "output_key" in _TEMPLATE_RESYNC_FIELDS, (
            "When a template changes its output_key (e.g. typo fix), "
            "`resync_agent_to_template` must propagate the change to the "
            "instantiated agent. Otherwise the agent stays on the stale "
            "output_key and downstream sub-agents read empty state."
        )

    def test_resync_includes_output_schema_name(self):
        from th2agent.core.superagents import _TEMPLATE_RESYNC_FIELDS

        assert "output_schema_name" in _TEMPLATE_RESYNC_FIELDS

    def test_resync_includes_skip_when_upstream(self):
        from th2agent.core.superagents import _TEMPLATE_RESYNC_FIELDS

        assert "skip_when_upstream" in _TEMPLATE_RESYNC_FIELDS

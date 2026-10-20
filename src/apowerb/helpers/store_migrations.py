"""Store table creation (agents, tools, skills, hub, api keys).

These ``create_table()`` calls used to live at module level in the store
modules: importing ``apowerb.core.agent_main`` or
``apowerb.tools_store.tools_helpers`` would open a connection and execute DDL
against the database — the most disruptive side-effect in the package, and a
clear obstacle to using it as a library.

They are grouped here and called from ``main.bootstrap()``, alongside the
other ``ensure_*`` functions. Server behaviour is unchanged: tables are still
guaranteed to exist before the first request is served.
"""

from __future__ import annotations

from apowerb.configs.th2logger import setup_logging

_logger = setup_logging(__name__)


def ensure_store_tables() -> None:
    """Create any missing store tables (no-op if they already exist)."""
    from apowerb.core.agent_main import agent_store
    from apowerb.core.api_key_main import api_key_store
    from apowerb.core.hub_main import hub_store
    from apowerb.core.run_main import run_store
    from apowerb.core.workflow_file_triggers import seen_file_store
    from apowerb.core.workflow_main import workflow_store
    from apowerb.core.workflow_triggers import workflow_trigger_store
    from apowerb.skills_store.skill_manager import skill_store
    from apowerb.tools_store.tools_helpers import tool_config_store

    for store in (
        agent_store,
        tool_config_store,
        skill_store,
        hub_store,
        api_key_store,
        run_store,
        workflow_store,
        workflow_trigger_store,
        seen_file_store,
    ):
        try:
            store.create_table()
        except Exception as exc:  # pragma: no cover - depends on the database
            _logger.error(
                "failed to create table for %s: %s",
                type(store).__name__,
                exc,
            )
            raise
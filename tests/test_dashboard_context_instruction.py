"""Le bloc « Dashboard context » de la consigne n'est ajouté qu'avec les outils
qu'il nomme (AGENT_DASHBOARD_ID posé au chargement de l'agent)."""
import asyncio
import sys
from pathlib import Path

import pytest

BENCH = Path(__file__).resolve().parents[1] / "bench" / "bi_tokens"


@pytest.fixture
def build(monkeypatch):
    from apowerb.core.agent_helpers import agent_utils, extras_loader

    monkeypatch.setattr(agent_utils, "get_agent_details", agent_utils.get_agent_details)
    monkeypatch.setattr(extras_loader, "_owner_category_params", extras_loader._owner_category_params)
    sys.path.insert(0, str(BENCH))
    try:
        import replay
    finally:
        sys.path.remove(str(BENCH))

    def _build():
        agent = replay.build_agent()
        return agent.instruction, {t.name for t in asyncio.run(agent.canonical_tools())}

    return _build


def test_no_dashboard_context_without_the_dashboard_tools(build, monkeypatch):
    monkeypatch.delenv("AGENT_DASHBOARD_ID", raising=False)
    instruction, tools = build()
    assert "## Dashboard context" not in instruction
    assert "tool_get_dashboard_data" not in instruction and "tool_get_dashboard_data" not in tools
    assert "request_user_input" in instruction  # le reste de la consigne interactive reste


def test_dashboard_context_comes_with_the_dashboard_tools(build, monkeypatch):
    monkeypatch.setenv("AGENT_DASHBOARD_ID", "e3ca56c3-3fc2-4e86-93ba-f51b5b8dd33d")
    instruction, tools = build()
    assert "## Dashboard context" in instruction
    assert {"tool_get_dashboard_data", "tool_add_chart_to_dashboard"} <= tools

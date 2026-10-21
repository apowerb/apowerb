"""Les gabarits BI coupent l'ajout automatique des outils de chat qu'ils
n'utilisent jamais (``suppressed_auto_tools``) ; les autres agents les gardent."""
import asyncio
import sys
from pathlib import Path

import pytest

from apowerb.core.superagents import get_superagent_template
from apowerb.core.superagents.templates.dashboard import BI_SUPPRESSED_AUTO_TOOLS

BENCH = Path(__file__).resolve().parents[1] / "bench" / "bi_tokens"


@pytest.fixture
def bench(monkeypatch):
    from apowerb.core.agent_helpers import agent_utils, extras_loader

    monkeypatch.setattr(agent_utils, "get_agent_details", agent_utils.get_agent_details)
    monkeypatch.setattr(extras_loader, "_owner_category_params", extras_loader._owner_category_params)
    monkeypatch.delenv("AGENT_DASHBOARD_ID", raising=False)
    sys.path.insert(0, str(BENCH))
    try:
        import replay
    finally:
        sys.path.remove(str(BENCH))
    return replay


def _tool_names(bench, monkeypatch, **row):
    base = bench._agent_row()
    monkeypatch.setattr(bench, "_agent_row", lambda: {**base, **row})
    agent = bench.build_agent()
    return {t.name for t in asyncio.run(agent.canonical_tools())}


@pytest.mark.parametrize("template_id", ["forecasting_agent", "dashboard_agent", "text_to_sql_agent"])
def test_bi_templates_suppress_the_unused_chat_tools(template_id):
    tpl = get_superagent_template(template_id)
    assert tpl["suppressed_auto_tools"] == BI_SUPPRESSED_AUTO_TOOLS
    recommended = {path.rsplit(".", 1)[-1] for path in tpl["recommended_tools"]}
    assert not recommended & set(BI_SUPPRESSED_AUTO_TOOLS)
    assert not [n for n in BI_SUPPRESSED_AUTO_TOOLS if n in tpl["agent_instruction"]]


@pytest.mark.parametrize("template_id", ["forecasting_agent", "dashboard_agent", "text_to_sql_agent"])
def test_bi_templates_compact_previous_turn_results(template_id):
    assert get_superagent_template(template_id)["compact_previous_turn_results"] is True


def test_bi_agent_is_built_without_them(bench, monkeypatch):
    names = _tool_names(bench, monkeypatch)
    assert not names & set(BI_SUPPRESSED_AUTO_TOOLS)
    # les cartes d'action et le widget servent en BI : ils restent
    assert {"request_user_input", "confirm_destructive", "embed_chart", "read_uploaded_file"} <= names


def test_agent_without_template_keeps_them(bench, monkeypatch):
    names = _tool_names(bench, monkeypatch, superagent_template_id=None)
    assert set(BI_SUPPRESSED_AUTO_TOOLS) <= names


def test_a_tool_configured_by_the_user_is_kept(bench, monkeypatch):
    names = _tool_names(bench, monkeypatch, agent_tools='["basic.tool_pdf_to_images"]')
    assert "tool_pdf_to_images" in names
    assert "request_payment" not in names

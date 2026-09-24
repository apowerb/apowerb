"""Tests for the jev_decision_agent SuperAgent template."""

from __future__ import annotations

import importlib

from apowerb.core.superagents import SUPERAGENT_TEMPLATES


def _template():
    return next(
        (t for t in SUPERAGENT_TEMPLATES if t["template_id"] == "jev_decision_agent"),
        None,
    )


def test_template_is_listed():
    assert _template() is not None


def test_required_fields_present():
    tpl = _template()
    for field in (
        "template_id",
        "name",
        "display_name",
        "description",
        "icon",
        "category",
        "agent_model",
        "agent_model_params",
        "agent_instruction",
        "agent_description",
        "recommended_tools",
        "memory_enabled",
        "artifacts_enabled",
        "tags",
        "readme",
    ):
        assert field in tpl and tpl[field] is not None, field


def test_recommends_the_jev_tools():
    tools = _template()["recommended_tools"]
    for name in ("jev.tool_jev_classify", "jev.tool_jev_decide", "jev.tool_jev_score"):
        assert name in tools


def test_every_recommended_tool_resolves_to_a_portfolio_function():
    for dotted in _template()["recommended_tools"]:
        module, func = dotted.rsplit(".", 1)
        mod = importlib.import_module(f"apowerb.tools_store.portfolio.{module}")
        assert callable(getattr(mod, func, None)), dotted


def test_instruction_names_every_jev_tool_and_the_escalation_rule():
    instruction = _template()["agent_instruction"]
    for name in ("tool_jev_classify", "tool_jev_decide", "tool_jev_score"):
        assert name in instruction
    assert "uncertain" in instruction


def test_readme_says_how_to_configure_the_key():
    assert "JEV_API_KEY" in _template()["readme"]

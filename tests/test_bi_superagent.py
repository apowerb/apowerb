"""Tests for dashboard_agent SuperAgent template."""

import pytest

from th2agent.core.superagents import SUPERAGENT_TEMPLATES


def _get_dashboard_template():
    """Find the dashboard_agent template in the registry."""
    for t in SUPERAGENT_TEMPLATES:
        if t["template_id"] == "dashboard_agent":
            return t
    return None


class TestDashboardAgentTemplate:
    def test_template_exists(self):
        template = _get_dashboard_template()
        assert template is not None, "dashboard_agent template not found in SUPERAGENT_TEMPLATES"

    def test_template_has_required_fields(self):
        template = _get_dashboard_template()
        required_fields = [
            "template_id",
            "name",
            "display_name",
            "description",
            "icon",
            "category",
            "agent_model",
            "agent_instruction",
            "agent_description",
            "agent_model_params",
            "recommended_tools",
            "memory_enabled",
            "artifacts_enabled",
            "tags",
            "readme",
        ]
        for field in required_fields:
            assert field in template, f"Template missing required field: {field}"
            assert template[field] is not None or field == "guardrails_config", (
                f"Field '{field}' must not be None"
            )

    def test_recommended_tools_format(self):
        template = _get_dashboard_template()
        tools = template["recommended_tools"]
        assert isinstance(tools, list), "recommended_tools must be a list"
        assert len(tools) > 0, "recommended_tools must not be empty"

        # All BI tools must be present
        bi_tools = [t for t in tools if t.startswith("business_intelligence.")]
        assert len(bi_tools) >= 5, (
            f"Expected at least 5 business_intelligence tools, got {len(bi_tools)}: {bi_tools}"
        )

        expected_bi_tools = [
            "business_intelligence.tool_create_chart",
            "business_intelligence.tool_create_dashboard",
            "business_intelligence.tool_add_chart_to_dashboard",
            "business_intelligence.tool_add_kpi_to_dashboard",
            "business_intelligence.tool_publish_dashboard",
        ]
        for tool in expected_bi_tools:
            assert tool in tools, f"Missing expected BI tool: {tool}"

    def test_template_has_skills(self):
        template = _get_dashboard_template()
        assert "agent_skills" in template, "Template must have agent_skills"
        skills = template["agent_skills"]
        assert isinstance(skills, list), "agent_skills must be a list"
        assert "dashboard-builder" in skills, "agent_skills must include 'dashboard-builder'"

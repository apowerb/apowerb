"""Lot A4 : templates dashboard_agent et forecasting_agent branchent la
prévision (outils + skills)."""
from apowerb.core.superagents.templates import SUPERAGENT_TEMPLATES


def _template(template_id):
    return next(t for t in SUPERAGENT_TEMPLATES if t["template_id"] == template_id)


class TestDashboardAgentTemplate:
    def test_has_forecast_tools(self):
        tpl = _template("dashboard_agent")
        tools = tpl["recommended_tools"]
        assert "bi_datasets.tool_list_datasets" in tools
        assert "bi_datasets.tool_describe_dataset" in tools
        assert "business_intelligence.tool_create_forecast_chart" in tools

    def test_has_forecasting_skill(self):
        tpl = _template("dashboard_agent")
        assert "forecasting" in tpl["agent_skills"]


class TestForecastingAgentTemplate:
    def test_has_forecast_tools(self):
        tpl = _template("forecasting_agent")
        tools = tpl["recommended_tools"]
        assert "bi_datasets.tool_list_datasets" in tools
        assert "bi_datasets.tool_describe_dataset" in tools
        assert "business_intelligence.tool_create_forecast_chart" in tools
        assert "api_call.tool_thaink2_forecast" in tools

    def test_has_both_skills(self):
        tpl = _template("forecasting_agent")
        assert "forecasting" in tpl["agent_skills"]
        assert "data-visualization" in tpl["agent_skills"]

    def test_instructions_mention_imported_datasets_before_sql(self):
        tpl = _template("forecasting_agent")
        instruction = tpl["agent_instruction"].lower()
        ds_pos = instruction.find("tool_list_datasets")
        sql_pos = instruction.find("sql")
        assert ds_pos != -1
        assert ds_pos < sql_pos


class TestLegacyForecastToolRemoval:
    def test_basic_module_no_longer_defines_the_legacy_tool(self):
        from apowerb.tools_store.portfolio import basic

        assert not hasattr(basic, "tool_thaink2_forecast")

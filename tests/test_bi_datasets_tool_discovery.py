"""Le module bi_datasets doit être découvert par l'introspection des
tools_store (tool_manager.get_tools_in_category)."""
from apowerb.tools_store.tool_manager import ToolsStore


def test_bi_datasets_tools_are_discovered():
    store = ToolsStore()
    assert "bi_datasets" in store.get_categories()
    tools = store.get_tools_in_category("bi_datasets")
    assert "bi_datasets.tool_list_datasets" in tools
    assert "bi_datasets.tool_describe_dataset" in tools


def test_forecast_chart_tool_discovered_in_business_intelligence():
    store = ToolsStore()
    tools = store.get_tools_in_category("business_intelligence")
    assert "business_intelligence.tool_create_forecast_chart" in tools

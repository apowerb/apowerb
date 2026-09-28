"""Lot A4 : inject_bi_dashboard_tools ajoute les outils de prévision."""

from apowerb.core.agent_helpers import extras_loader


def test_dashboard_context_injects_dataset_and_forecast_tools(monkeypatch):
    monkeypatch.setenv("AGENT_DASHBOARD_ID", "a4211153-ca13-4888-b34c-114cd8fab6b9")
    names, funcs = [], []

    extras_loader.inject_bi_dashboard_tools("agent-test", names, funcs, "owner-test")

    assert "bi_datasets.tool_list_datasets" in names
    assert "bi_datasets.tool_describe_dataset" in names
    assert "business_intelligence.tool_create_forecast_chart" in names


def test_no_dashboard_context_injects_nothing(monkeypatch):
    monkeypatch.delenv("AGENT_DASHBOARD_ID", raising=False)
    names, funcs = [], []

    extras_loader.inject_bi_dashboard_tools("agent-test", names, funcs, "owner-test")

    assert names == []
    assert funcs == []

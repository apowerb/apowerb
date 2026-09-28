"""Tests pour business_intelligence.tool_create_forecast_chart (lot A2)."""
import asyncio
import contextlib
from unittest.mock import MagicMock, patch

import pytest

import apowerb.bi.db_stores as db_stores
import apowerb.tools_store.portfolio.business_intelligence as bi
from apowerb.bi.charts.core import ChartOrigin, ChartType, SourceType
from apowerb.bi.charts.service import InMemoryChartStore

OWNER = "owner@example.com"


@pytest.fixture(autouse=True)
def _patch_session(monkeypatch):
    @contextlib.asynccontextmanager
    async def no_db():
        yield None

    monkeypatch.setattr(bi, "_get_session", no_db)
    monkeypatch.setenv("AGENT_OWNER", OWNER)
    yield


def _loaded(columns=("date", "region", "sales"), truncated=False):
    return {
        "success": True,
        "rows": [
            {"date": "2024-01-01", "sales": 90},
            {"date": "2024-02-01", "sales": 95},
        ],
        "truncated": truncated,
        "columns": list(columns),
        "name": "f.csv",
    }


_TH2FORECAST_RESPONSE = {
    "status": "success",
    "frequency": "month",
    "warnings": [],
    "series": [{
        "group": None, "model": "prophet", "reliability": "good",
        "beats_baseline": True, "metrics": {},
        "forecast": [{"date": "2024-03-01", "value": 100.0}],
        "warnings": [],
    }],
}


def _run(coro):
    return asyncio.run(coro)


class TestOwnerAndColumnChecks:
    def test_owner_mismatch_is_not_found_and_no_chart_created(self, monkeypatch):
        store = InMemoryChartStore()
        monkeypatch.setattr(db_stores, "DatabaseChartStore", lambda db, owner=None: store)
        with patch.object(bi, "_load_owned_dataset_rows") as mock_load:
            mock_load.return_value = {"success": False, "error": "Jeu de données introuvable."}
            result = bi.tool_create_forecast_chart(
                dataset_id="ds1", date_var="date", target_var="sales",
                horizon=3, title="Ventes prévues",
            )
        assert result["success"] is False
        assert "introuvable" in result["error"].lower()
        assert _run(store.list_all()) == []

    def test_unknown_column_lists_available_columns_and_no_chart_created(self, monkeypatch):
        store = InMemoryChartStore()
        monkeypatch.setattr(db_stores, "DatabaseChartStore", lambda db, owner=None: store)
        with patch.object(bi, "_load_owned_dataset_rows") as mock_load:
            mock_load.return_value = _loaded(columns=["date", "sales"])
            result = bi.tool_create_forecast_chart(
                dataset_id="ds1", date_var="date", target_var="revenue_missing",
                horizon=3, title="Ventes prévues",
            )
        assert result["success"] is False
        assert "revenue_missing" in result["error"]
        assert _run(store.list_all()) == []


class TestChartCreation:
    def test_creates_forecast_chart_with_full_limit_and_expected_config(self, monkeypatch):
        store = InMemoryChartStore()
        monkeypatch.setattr(db_stores, "DatabaseChartStore", lambda db, owner=None: store)

        with patch.object(bi, "_load_owned_dataset_rows") as mock_load, \
             patch.object(bi.api_call, "Th2forecastClient") as mock_ctor:
            mock_load.return_value = _loaded()
            mock_instance = MagicMock()
            mock_instance.forecast.return_value = _TH2FORECAST_RESPONSE
            mock_ctor.return_value = mock_instance

            result = bi.tool_create_forecast_chart(
                dataset_id="ds1", date_var="date", target_var="sales",
                horizon=3, title="Ventes prévues", group_var="region", frequency="month",
            )

        assert result["success"] is True
        chart = _run(store.get(result["chart_id"]))
        assert chart.chart_type is ChartType.FORECAST
        assert chart.source.source_type is SourceType.CSV
        assert chart.source.query == "csv://ds1"
        assert chart.source.limit == 100_000
        assert chart.config["date_var"] == "date"
        assert chart.config["target_var"] == "sales"
        assert chart.config["group_var"] == "region"
        assert chart.config["horizon"] == 3
        assert chart.config["frequency"] == "month"
        assert chart.config["models"] == ["auto"]
        assert chart.config["confidence_levels"] == [0.8, 0.95]
        assert chart.origin is ChartOrigin.CHAT
        assert result["summary"]["status"] == "success"

    def test_forecast_failure_creates_no_chart(self, monkeypatch):
        store = InMemoryChartStore()
        monkeypatch.setattr(db_stores, "DatabaseChartStore", lambda db, owner=None: store)

        with patch.object(bi, "_load_owned_dataset_rows") as mock_load, \
             patch.object(bi.api_call, "Th2forecastClient") as mock_ctor:
            mock_load.return_value = _loaded()
            mock_ctor.side_effect = Exception("service indisponible")

            result = bi.tool_create_forecast_chart(
                dataset_id="ds1", date_var="date", target_var="sales",
                horizon=3, title="Ventes prévues",
            )

        assert result["success"] is False
        assert _run(store.list_all()) == []

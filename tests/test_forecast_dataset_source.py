"""Tests pour l'extension dataset_id de api_call.tool_thaink2_forecast (lot A2).

Complète tests/test_forecast_tool.py (sql/rows) sans le modifier : ces tests
couvrent uniquement la nouvelle source dataset_id.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from apowerb.tools_store.portfolio import api_call


_TH2FORECAST_RESPONSE = {
    "status": "success",
    "frequency": "month",
    "warnings": [],
    "series": [
        {
            "group": None,
            "model": "prophet",
            "reliability": "good",
            "beats_baseline": True,
            "metrics": {"mape": 0.08, "smape": 0.079, "mase": 0.7, "rmse": 11.2},
            "forecast": [
                {"date": "2025-01-01", "value": 100.0},
                {"date": "2025-02-01", "value": 110.0},
            ],
            "warnings": [],
        }
    ],
}


def _dataset_kwargs(**overrides):
    kwargs = dict(date_var="date", target_var="sales", horizon=3, dataset_id="ds1")
    kwargs.update(overrides)
    return kwargs


class TestDatasetIdSource:
    def test_dataset_id_counts_as_the_one_source(self):
        with patch.object(api_call, "_load_owned_dataset_rows") as mock_load:
            mock_load.return_value = {
                "success": False,
                "error": "Jeu de données introuvable.",
            }
            with patch.object(api_call, "_agent_owner", return_value="me@example.com"):
                result = api_call.tool_thaink2_forecast(**_dataset_kwargs())
        assert result["status"] == "error"
        assert "introuvable" in result["message"].lower()

    def test_rejects_dataset_id_combined_with_rows(self):
        result = api_call.tool_thaink2_forecast(
            date_var="date", target_var="sales", horizon=3,
            dataset_id="ds1", rows=[{"date": "2024-01-01", "sales": 1}],
        )
        assert result["status"] == "error"

    def test_no_owner_context_is_an_error(self):
        with patch.object(api_call, "_agent_owner", return_value=""):
            result = api_call.tool_thaink2_forecast(**_dataset_kwargs())
        assert result["status"] == "error"

    def test_unknown_column_lists_available_columns(self):
        with patch.object(api_call, "_agent_owner", return_value="me@example.com"), \
             patch.object(api_call, "_load_owned_dataset_rows") as mock_load:
            mock_load.return_value = {
                "success": True,
                "rows": [{"date": "2024-01-01", "sales": 1}],
                "truncated": False,
                "columns": ["date", "sales"],
                "name": "f.csv",
            }
            result = api_call.tool_thaink2_forecast(
                **_dataset_kwargs(target_var="revenue_missing")
            )
        assert result["status"] == "error"
        assert "revenue_missing" in result["message"]
        assert "date" in result["message"] and "sales" in result["message"]

    def test_beyond_cap_refuses_explicitly_not_silently(self):
        with patch.object(api_call, "_agent_owner", return_value="me@example.com"), \
             patch.object(api_call, "_load_owned_dataset_rows") as mock_load:
            mock_load.return_value = {
                "success": True,
                "rows": [{"date": "2024-01-01", "sales": 1}] * 5,
                "truncated": True,
                "columns": ["date", "sales"],
                "name": "f.csv",
            }
            result = api_call.tool_thaink2_forecast(**_dataset_kwargs())
        assert result["status"] == "error"
        assert "100000" in result["message"] or "100 000" in result["message"]

    def test_valid_dataset_id_forecasts_successfully(self):
        with patch.object(api_call, "_agent_owner", return_value="me@example.com"), \
             patch.object(api_call, "_load_owned_dataset_rows") as mock_load, \
             patch.object(api_call, "Th2forecastClient") as mock_ctor:
            mock_load.return_value = {
                "success": True,
                "rows": [
                    {"date": "2024-01-01", "sales": 90},
                    {"date": "2024-02-01", "sales": 95},
                ],
                "truncated": False,
                "columns": ["date", "sales"],
                "name": "f.csv",
            }
            mock_instance = MagicMock()
            mock_instance.forecast.return_value = _TH2FORECAST_RESPONSE
            mock_ctor.return_value = mock_instance

            result = api_call.tool_thaink2_forecast(**_dataset_kwargs())

        assert result["status"] == "success"
        assert len(result["series"]) == 1

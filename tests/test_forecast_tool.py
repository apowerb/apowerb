"""Unit tests for tools_store.portfolio.api_call.tool_thaink2_forecast — the
typed agent tool that replaced tool_thaink2_forecast(api_url, payload, token).

Covers: argument validation (source, horizon, models), the compact-summary
shape built from a th2forecast response, and error propagation.
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
            "metrics": {"mape": 0.08, "smape": 0.079, "mase": 0.7, "rmse": 11.2, "holdout_points": 6},
            "forecast": [
                {"date": "2025-01-01", "value": 100.0, "lower_80": 90.0, "upper_80": 110.0, "lower_95": 85.0, "upper_95": 115.0},
                {"date": "2025-02-01", "value": 110.0, "lower_80": 98.0, "upper_80": 122.0, "lower_95": 92.0, "upper_95": 128.0},
                {"date": "2025-03-01", "value": 130.0, "lower_80": 115.0, "upper_80": 145.0, "lower_95": 108.0, "upper_95": 152.0},
            ],
            "warnings": [],
        }
    ],
}


def _valid_kwargs(**overrides):
    kwargs = dict(
        date_var="date",
        target_var="sales",
        horizon=3,
        rows=[{"date": "2024-01-01", "sales": 90}, {"date": "2024-02-01", "sales": 95}],
    )
    kwargs.update(overrides)
    return kwargs


class TestArgumentValidation:
    def test_requires_exactly_one_data_source(self):
        result = api_call.tool_thaink2_forecast(date_var="date", target_var="sales", horizon=3)
        assert result["status"] == "error"
        assert "sql" in result["message"] and "rows" in result["message"]

    def test_rejects_both_sql_and_rows(self):
        result = api_call.tool_thaink2_forecast(
            date_var="date", target_var="sales", horizon=3, sql="SELECT 1", rows=[{"a": 1}]
        )
        assert result["status"] == "error"

    def test_caps_inline_rows(self):
        too_many = [{"date": "2024-01-01", "sales": 1}] * (api_call._MAX_INLINE_ROWS + 1)
        result = api_call.tool_thaink2_forecast(**_valid_kwargs(rows=too_many))
        assert result["status"] == "error"
        assert str(api_call._MAX_INLINE_ROWS) in result["message"]

    def test_rejects_horizon_out_of_range(self):
        result = api_call.tool_thaink2_forecast(**_valid_kwargs(horizon=0))
        assert result["status"] == "error"
        assert "horizon" in result["message"]

    def test_rejects_unknown_model(self):
        result = api_call.tool_thaink2_forecast(**_valid_kwargs(models=["xgboost"]))
        assert result["status"] == "error"
        assert "xgboost" in result["message"]

    def test_rejects_empty_data(self):
        result = api_call.tool_thaink2_forecast(**_valid_kwargs(rows=[]))
        assert result["status"] == "error"

    def test_sql_source_runs_through_tool_run_sql(self):
        with patch.object(
            api_call, "tool_run_sql",
            return_value={"success": True, "data": [{"date": "2024-01-01", "sales": 90}]},
        ) as mock_run_sql, patch.object(api_call, "Th2forecastClient") as mock_ctor:
            mock_instance = MagicMock()
            mock_instance.forecast.return_value = _TH2FORECAST_RESPONSE
            mock_ctor.return_value = mock_instance

            result = api_call.tool_thaink2_forecast(
                date_var="date", target_var="sales", horizon=3, sql="SELECT date, sales FROM monthly_sales"
            )

        mock_run_sql.assert_called_once_with("SELECT date, sales FROM monthly_sales")
        assert result["status"] == "success"

    def test_sql_failure_is_reported(self):
        with patch.object(
            api_call, "tool_run_sql",
            return_value={"success": False, "error": "colonne inconnue"},
        ):
            result = api_call.tool_thaink2_forecast(
                date_var="date", target_var="sales", horizon=3, sql="SELECT bogus FROM t"
            )
        assert result["status"] == "error"
        assert "colonne inconnue" in result["message"]


class TestCompactSummary:
    def test_success_returns_compact_series_summary(self):
        with patch.object(api_call, "Th2forecastClient") as mock_ctor:
            mock_instance = MagicMock()
            mock_instance.forecast.return_value = _TH2FORECAST_RESPONSE
            mock_ctor.return_value = mock_instance

            result = api_call.tool_thaink2_forecast(**_valid_kwargs())

        assert result["status"] == "success"
        assert len(result["series"]) == 1
        series = result["series"][0]

        assert series["model"] == "prophet"
        assert series["reliability"] == "good"
        assert series["beats_baseline"] is True
        assert series["metrics"] == {"mape": 0.08, "smape": 0.079, "mase": 0.7, "rmse": 11.2}
        assert series["summary"]["trend"] == "hausse"
        assert series["summary"]["forecast_min"] == 100.0
        assert series["summary"]["forecast_max"] == 130.0
        assert series["summary"]["avg_interval_width"] > 0
        # The full curve is not dumped: sampled points, not the raw series.
        assert len(series["forecast_sample"]) <= 6

    def test_th2forecast_api_error_is_reported_with_errors(self):
        from apowerb.integrations.th2forecast_client import Th2forecastAPIError

        error_body = {"status": "error", "errors": [{"field": "date_var", "message": "colonne absente"}]}
        with patch.object(api_call, "Th2forecastClient") as mock_ctor:
            mock_instance = MagicMock()
            mock_instance.forecast.side_effect = Th2forecastAPIError(400, error_body)
            mock_ctor.return_value = mock_instance

            result = api_call.tool_thaink2_forecast(**_valid_kwargs())

        assert result["status"] == "error"
        assert result["errors"] == error_body["errors"]

    def test_not_configured_is_reported_clearly(self):
        from apowerb.integrations.th2forecast_client import Th2forecastNotConfigured

        with patch.object(api_call, "Th2forecastClient", side_effect=Th2forecastNotConfigured("no url")):
            result = api_call.tool_thaink2_forecast(**_valid_kwargs())

        assert result["status"] == "error"
        assert "TH2FORECAST_URL" in result["message"]

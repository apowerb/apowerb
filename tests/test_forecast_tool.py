"""Unit tests for tools_store.portfolio.api_call.tool_thaink2_forecast — the
typed agent tool that replaced tool_thaink2_forecast(api_url, payload, token).

Covers: argument validation (source, horizon, models), the compact-summary
shape built from a th2forecast response, and error propagation.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

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
        result = api_call.tool_thaink2_forecast(**_valid_kwargs(models=["lstm"]))
        assert result["status"] == "error"
        assert "lstm" in result["message"]

    def test_rejects_empty_data(self):
        result = api_call.tool_thaink2_forecast(**_valid_kwargs(rows=[]))
        assert result["status"] == "error"

    def test_sql_source_runs_through_the_agent_connection(self, monkeypatch):
        monkeypatch.setenv("AGENT_OWNER", "owner@example.com")
        loaded = {"success": True, "rows": [{"date": "2024-01-01", "sales": 90}], "truncated": False,
                  "columns": ["date", "sales"], "connection_config_id": "tool_config7"}
        with patch.object(
            api_call, "_load_agent_sql_rows", new=AsyncMock(return_value=loaded),
        ) as mock_load, patch.object(api_call, "Th2forecastClient") as mock_ctor:
            mock_instance = MagicMock()
            mock_instance.forecast.return_value = _TH2FORECAST_RESPONSE
            mock_ctor.return_value = mock_instance

            result = api_call.tool_thaink2_forecast(
                date_var="date", target_var="sales", horizon=3, sql="SELECT date, sales FROM monthly_sales"
            )

        assert mock_load.await_args.args[:2] == ("SELECT date, sales FROM monthly_sales", "owner@example.com")
        assert result["status"] == "success"

    def test_sql_failure_is_reported(self, monkeypatch):
        monkeypatch.setenv("AGENT_OWNER", "owner@example.com")
        with patch.object(
            api_call, "_load_agent_sql_rows",
            new=AsyncMock(return_value={"success": False, "error": "colonne inconnue"}),
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

    def test_trend_compares_with_the_same_period_last_year(self):
        # Seasonal weekly series: the horizon starts right after the autumn
        # peak and drifts down within itself, yet sits ~7 % above the same
        # weeks last year. First vs last forecast point said "baisse".
        from datetime import date, timedelta

        start = date(2025, 1, 6)
        history = [
            {"date": (start + timedelta(weeks=i)).isoformat(), "value": 6000.0 + (2500.0 if i >= 40 else 0.0)}
            for i in range(52)
        ]
        fc_start = start + timedelta(weeks=52)
        forecast = [
            {"date": (fc_start + timedelta(weeks=i)).isoformat(), "value": 6670.0 - 20.0 * i}
            for i in range(26)
        ]
        summary = api_call._summarize_series({"history": history, "forecast": forecast})["summary"]

        assert summary["trend"] == "hausse"
        assert summary["trend_basis"] == "même période l'an dernier"
        assert 6.0 < summary["change_pct"] < 8.0

    def test_trend_falls_back_to_the_horizon_without_a_year_of_history(self):
        series = dict(_TH2FORECAST_RESPONSE["series"][0])
        series["history"] = [{"date": "2024-12-01", "value": 95.0}]
        summary = api_call._summarize_series(series)["summary"]

        assert summary["trend"] == "hausse"
        assert summary["trend_basis"] == "début et fin de l'horizon"

    def test_unparsable_history_dates_do_not_break_the_summary(self):
        series = dict(_TH2FORECAST_RESPONSE["series"][0])
        series["history"] = [{"date": "not a date", "value": 1.0}, {"date": None, "value": 2.0}]
        summary = api_call._summarize_series(series)["summary"]

        assert summary["trend"] == "hausse"
        assert summary["trend_basis"] == "début et fin de l'horizon"

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


def _sent_body(mock_instance) -> dict:
    return mock_instance.forecast.call_args.args[0]


class TestOutlierCorrection:
    def _run(self, **overrides):
        with patch.object(api_call, "Th2forecastClient") as mock_ctor:
            mock_instance = MagicMock()
            mock_instance.forecast.return_value = _TH2FORECAST_RESPONSE
            mock_ctor.return_value = mock_instance
            result = api_call.tool_thaink2_forecast(**_valid_kwargs(**overrides))
        return result, mock_instance

    def test_correct_outliers_sends_preprocessing_outliers_true(self):
        result, client = self._run(correct_outliers=True)
        assert result["status"] == "success"
        assert _sent_body(client)["preprocessing"] == {"outliers": True}

    def test_default_sends_no_preprocessing(self):
        _, client = self._run()
        assert not _sent_body(client).get("preprocessing")

    def test_explicit_false_sends_no_preprocessing(self):
        _, client = self._run(correct_outliers=False)
        assert not _sent_body(client).get("preprocessing")

    def test_summary_exposes_preprocessing_counts_and_caps_corrections_at_five(self):
        corrections = [
            {"date": f"2022-0{i}-01", "original": 400 + i, "corrected": 100.0 + i, "kind": "anomaly"}
            for i in range(1, 8)
        ]
        response = {
            **_TH2FORECAST_RESPONSE,
            "series": [{
                **_TH2FORECAST_RESPONSE["series"][0],
                "preprocessing": {
                    "anomalies_corrected": 7, "outliers_corrected": 2, "corrections": corrections,
                },
            }],
        }
        with patch.object(api_call, "Th2forecastClient") as mock_ctor:
            mock_ctor.return_value = MagicMock(forecast=MagicMock(return_value=response))
            result = api_call.tool_thaink2_forecast(**_valid_kwargs(correct_outliers=True))

        block = result["series"][0]["preprocessing"]
        assert block["anomalies_corrected"] == 7
        assert block["outliers_corrected"] == 2
        assert block["corrections"] == corrections[:5]

    def test_summary_omits_preprocessing_key_when_response_has_none(self):
        result, _ = self._run()
        assert "preprocessing" not in result["series"][0]

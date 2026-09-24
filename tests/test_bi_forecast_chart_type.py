"""The dashboard forecast widget is stored with chart_type="forecast"."""
from apowerb.bi.charts.core import ChartType
from apowerb.bi.charts.schemas import ChartUpdateRequest


def test_forecast_is_a_chart_type():
    assert ChartType("forecast") is ChartType.FORECAST


def test_a_chart_can_be_switched_to_forecast():
    assert ChartUpdateRequest(chart_type="forecast").chart_type is ChartType.FORECAST

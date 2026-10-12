"""Tests pour le tableau de bord de prévision : tables calculées
(apowerb.bi.forecast_dashboard) et outil tool_create_forecast_dashboard."""
import asyncio
import contextlib
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest

import apowerb.bi.db_stores as db_stores
import apowerb.tools_store.portfolio.business_intelligence as bi
from apowerb.bi import forecast_dashboard as fd
from apowerb.bi.charts.core import ChartType, SourceType
from apowerb.bi.charts.service import InMemoryChartStore
from apowerb.bi.dashboards.service import InMemoryDashboardStore

OWNER = "owner@example.com"


def _weeks(start: date, values: list[float], band: float = 0.0) -> list[dict]:
    points = []
    for i, v in enumerate(values):
        p = {"date": (start + timedelta(weeks=i)).isoformat(), "value": v}
        if band:
            p.update(lower_80=v - band, upper_80=v + band, lower_95=v - 2 * band, upper_95=v + 2 * band)
        points.append(p)
    return points


def _series(group, history_values, forecast_values, reliability="good", band=10.0):
    # 4 semaines prévues en janvier 2026 ; l'historique couvre les mêmes
    # semaines de janvier 2025 (52 semaines avant).
    return {
        "group": group, "level": None, "model": "prophet", "reliability": reliability,
        "beats_baseline": reliability == "good", "metrics": {"mape": 5.0},
        "history": _weeks(date(2025, 1, 6), history_values),
        "forecast": _weeks(date(2026, 1, 5), forecast_values, band),
        "warnings": [],
    }


RESPONSE = {
    "status": "success",
    "frequency": "week",
    "warnings": [],
    "series": [
        _series("Cartons", [100, 100, 100, 100], [110, 110, 110, 110]),
        _series("Palettes", [50, 50, 50, 50], [45, 45, 60, 50], reliability="poor", band=20.0),
    ],
}


class TestTables:
    def test_series_table_has_one_row_per_series_with_last_year_and_reliability(self):
        table = fd.series_table(RESPONSE)

        assert [r["serie"] for r in table] == ["Cartons", "Palettes"]
        cartons, palettes = table
        assert cartons["prevu_total"] == 440
        assert cartons["meme_periode_n1"] == 400
        assert cartons["ecart_n1_pct"] == 10.0
        assert cartons["fiabilite"] == "Fiable"
        assert cartons["bat_la_prevision_naive"] == "oui"
        assert cartons["incertitude_80_pct"] == pytest.approx(18.2, abs=0.1)
        assert palettes["fiabilite"] == "Peu fiable"
        assert palettes["pic_prevu"] == "2026-01-19"

    def test_series_without_last_year_has_no_comparison(self):
        series = _series(None, [], [10, 12])
        table = fd.series_table({"series": [series]})

        assert table[0]["serie"] == "Total"
        assert table[0]["meme_periode_n1"] is None
        assert table[0]["ecart_n1_pct"] is None

    def test_period_table_is_wide_one_column_per_series(self):
        table = fd.series_table(RESPONSE)
        periods = fd.period_table(RESPONSE, table)

        assert len(periods) == 4
        assert periods[2] == {"periode": "2026-01-19", "Cartons": 110, "Palettes": 60}

    def test_headline_totals_change_reliable_and_peak(self):
        table = fd.series_table(RESPONSE)
        headline = fd.headline(table, fd.period_table(RESPONSE, table))

        assert headline["total"] == 640
        assert headline["change_pct"] == pytest.approx((640 - 600) / 600 * 100, abs=0.1)
        assert (headline["reliable"], headline["series"]) == (1, 2)
        assert (headline["peak_period"], headline["peak_value"]) == ("2026-01-19", 170)

    @pytest.mark.parametrize("frequency, expected", [
        ("week", "Semaine du 12/01/2026"),
        ("month", "01/2026"),
        ("quarter", "T1 2026"),
        ("year", "2026"),
        ("day", "12/01/2026"),
        (None, "12/01/2026"),
    ])
    def test_period_is_shown_for_a_reader(self, frequency, expected):
        assert fd.format_period("2026-01-12", frequency) == expected

    def test_unparsable_period_is_kept_as_is(self):
        assert fd.format_period("W12", "week") == "W12"

    def test_small_series_are_folded_into_others(self):
        many = {"series": [_series(f"S{i}", [], [100 - i]) for i in range(12)]}
        table = fd.series_table(many)

        shares = fd.share_table(table)
        assert len(shares) == fd.MAX_DISPLAYED_SERIES
        assert shares[-1]["serie"] == fd.OTHERS_LABEL
        assert shares[-1]["prevu_total"] == sum(100 - i for i in range(7, 12))
        periods = fd.period_table(many, table)
        assert set(periods[0]) == {"periode", *[f"S{i}" for i in range(7)], fd.OTHERS_LABEL}

    def test_hierarchy_levels_are_left_out(self):
        total = {**_series("Tous", [], [999]), "level": "total"}
        table = fd.series_table({"series": [total, _series("Cartons", [], [1])]})

        assert [r["serie"] for r in table] == ["Cartons"]


@pytest.fixture
def stores(monkeypatch):
    @contextlib.asynccontextmanager
    async def no_db():
        yield None

    monkeypatch.setattr(bi, "_get_session", no_db)
    monkeypatch.setenv("AGENT_OWNER", OWNER)
    charts, dashboards, datasets = InMemoryChartStore(), InMemoryDashboardStore(), {}
    monkeypatch.setattr(db_stores, "DatabaseChartStore", lambda db, owner=None: charts)
    monkeypatch.setattr(db_stores, "DatabaseDashboardStore", lambda db, owner=None: dashboards)

    async def store_rows(rows, name, owner_email, organization_id, project_id):
        dataset_id = f"ds-{len(datasets)}"
        datasets[dataset_id] = rows
        return dataset_id

    monkeypatch.setattr(bi, "_async_store_rows_dataset", store_rows)
    return charts, dashboards, datasets


def _loaded():
    return {
        "success": True, "truncated": False, "columns": ["semaine", "famille", "ventes"],
        "rows": [{"semaine": "2025-01-06", "famille": "Cartons", "ventes": 100}],
    }


def _create(response=RESPONSE, **extra):
    with patch.object(bi, "_load_owned_dataset_rows") as load, \
         patch.object(bi.api_call, "Th2forecastClient") as ctor:
        load.return_value = _loaded()
        client = MagicMock()
        client.forecast.return_value = response
        ctor.return_value = client
        result = bi.tool_create_forecast_dashboard(
            dataset_id="src", date_var="semaine", target_var="ventes", horizon=4,
            title="Prévision emballage", group_var="famille", frequency="week", **extra,
        )
    return result, client


def _run(coro):
    return asyncio.run(coro)


class TestTool:
    def test_builds_kpis_forecast_and_explaining_charts_from_one_forecast(self, stores):
        charts, dashboards, datasets = stores

        result, client = _create()

        assert result["success"] is True, result
        assert client.forecast.call_count == 1
        board = _run(dashboards.get(result["dashboard_id"]))
        assert board.visibility.value == "private"
        kpis = [c for c in board.components if c.component_type.value == "key_value"]
        assert [(c.position.row, c.position.col) for c in kpis] == [(0, 0), (0, 3), (0, 6), (0, 9)]
        assert [c.key_value.label for c in kpis] == [
            "Total prévu sur l'horizon", "Écart vs même période N-1", "Séries fiables", "Pic prévu",
        ]
        assert "« ventes »" in kpis[0].key_value.description
        assert kpis[3].key_value.value == "Semaine du 19/01/2026"
        widgets = [c for c in board.components if c.component_type.value == "chart"]
        assert widgets[0].chart.chart_id == result["forecast_chart_id"]
        assert (widgets[0].position.row, widgets[0].position.width) == (2, 12)
        forecast = _run(charts.get(result["forecast_chart_id"]))
        assert forecast.chart_type is ChartType.FORECAST
        assert forecast.source.query == "csv://src"

        made = [_run(charts.get(i)) for i in result["chart_ids"]]
        assert [c.chart_type.value for c in made] == ["bar", "donut", "bar", "bar", "table"]
        assert all(c.source.source_type is SourceType.CSV for c in made)
        comparison, _, per_period, _, detail = made
        assert comparison.config == {"labelColumn": "serie", "valueColumns": ["prevu_total", "meme_periode_n1"]}
        assert per_period.config["stacked"] is True
        assert datasets[per_period.source.query.removeprefix("csv://")][0] == {
            "periode": "2026-01-05", "Cartons": 110, "Palettes": 45,
        }
        assert len(datasets[detail.source.query.removeprefix("csv://")]) == 2
        assert result["headline"]["reliable"] == 1
        assert result["summary"]["series"][0]["summary"]["change_pct"] == 10.0

    def test_single_series_skips_the_share_donut(self, stores):
        single = {**RESPONSE, "series": [RESPONSE["series"][0]]}

        result, _ = _create(response=single)

        assert result["success"] is True, result
        types = [_run(stores[0].get(i)).chart_type.value for i in result["chart_ids"]]
        assert "donut" not in types

    def test_forecast_failure_creates_nothing(self, stores):
        charts, dashboards, datasets = stores
        failed = {"status": "error", "message": "Série trop courte."}

        with patch.object(bi.api_call, "_forecast_raw", return_value=failed), \
             patch.object(bi, "_load_owned_dataset_rows", return_value=_loaded()):
            result = bi.tool_create_forecast_dashboard(
                dataset_id="src", date_var="semaine", target_var="ventes", horizon=4, title="T",
            )

        assert result == {"success": False, "error": "Série trop courte."}
        assert _run(charts.list_all()) == []
        assert _run(dashboards.list(page=1, page_size=10))[1] == 0
        assert datasets == {}

    def test_taken_title_gets_a_timestamp(self, stores, monkeypatch):
        create = bi._async_create_dashboard
        titles = []

        async def unique_titles(title, **kwargs):
            # La base refuse un titre déjà pris (contrainte unique) ; le
            # magasin en mémoire ne le fait pas, on rejoue son erreur.
            if title in titles:
                raise RuntimeError('duplicate key value violates unique constraint "uq_bi_name"')
            titles.append(title)
            return await create(title=title, **kwargs)

        monkeypatch.setattr(bi, "_async_create_dashboard", unique_titles)
        first, _ = _create()
        second, _ = _create()

        assert second["success"] is True, second
        assert first["dashboard_title"] == "Prévision emballage"
        assert second["dashboard_title"].startswith("Prévision emballage (")
        assert first["dashboard_id"] != second["dashboard_id"]


def test_rows_are_stored_as_an_owned_csv_dataset(monkeypatch):
    @contextlib.asynccontextmanager
    async def no_db():
        yield None

    monkeypatch.setattr(bi, "_get_session", no_db)
    calls = []

    async def fake_store(store, **kwargs):
        calls.append((store, kwargs))
        return "key"

    monkeypatch.setattr("apowerb.bi.data.tabular_import.store_tabular_as_csv", fake_store)
    monkeypatch.setattr(db_stores, "DatabaseDataStore", lambda db, owner=None: f"store:{owner}")
    rows = [{"serie": "Cartons", "prevu_total": 440}]

    dataset_id = _run(bi._async_store_rows_dataset(rows, "T - Détail", OWNER, "org", "proj"))

    store, kwargs = calls[0]
    assert store == f"store:{OWNER}"
    assert kwargs["file_id"] == dataset_id
    assert kwargs["data"].columns == ["serie", "prevu_total"]
    assert kwargs["data"].rows == rows
    assert (kwargs["organization_id"], kwargs["project_id"], kwargs["uploaded_by"]) == ("org", "proj", OWNER)


def test_dashboard_failure_still_returns_the_saved_forecast_chart(stores, monkeypatch):
    async def refuse(**kwargs):
        raise RuntimeError("base indisponible")

    monkeypatch.setattr(bi, "_async_create_dashboard", refuse)

    result, _ = _create()

    assert result["success"] is False
    assert result["dashboard_id"] is None
    assert result["forecast_chart_id"]
    assert "non créé" in result["error"]

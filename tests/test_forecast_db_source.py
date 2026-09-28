"""Source base de données de la prévision.

Une requête SQL de prévision passe par la connexion base de l'agent
(tool_config de son propriétaire, via DatabaseQueryExecutor), jamais par
``database.tool_run_sql`` du module, qui se connecte avec les variables
``DB_*`` du processus : par défaut la base de la plateforme elle-même.
"""

from __future__ import annotations

import contextlib
import json
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import apowerb.bi.db_stores as db_stores
import apowerb.tools_store.portfolio.business_intelligence as bi
from apowerb.bi.charts.core import ChartType, DataSource, SourceType
from apowerb.bi.charts.service import InMemoryChartStore
from apowerb.schema.forecast_schema import MAX_DATA_ROWS
from apowerb.tools_store.portfolio import api_call, bi_datasets

OWNER = "owner@example.com"
CONFIG_ID = "tool_config7"
SQL = "SELECT day, store, amount FROM sales"

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


def _db_rows():
    """What asyncpg hands back: date objects and Decimals, not strings."""
    return [
        {"day": date(2024, 1, 1), "store": "Nord", "amount": Decimal("90.50")},
        {"day": datetime(2024, 2, 1), "store": "Nord", "amount": Decimal("95")},
    ]


@pytest.fixture(autouse=True)
def _agent_context(monkeypatch):
    monkeypatch.setenv("AGENT_OWNER", OWNER)


@pytest.fixture
def connection(monkeypatch):
    """The agent has a database tool_config owned by OWNER."""
    monkeypatch.setattr(bi_datasets, "_agent_db_connection", lambda owner=None: (CONFIG_ID, OWNER))


@pytest.fixture
def no_connection(monkeypatch):
    monkeypatch.setattr(bi_datasets, "_agent_db_connection", lambda owner=None: None)


@pytest.fixture
def executor():
    """Patched DatabaseQueryExecutor class; ``.run`` returns _db_rows()."""
    with patch("apowerb.bi.data.db_executor.DatabaseQueryExecutor") as cls:
        cls.return_value.run = AsyncMock(return_value=_db_rows())
        yield cls


def _forecast_client():
    client = MagicMock()
    client.forecast.return_value = _TH2FORECAST_RESPONSE
    return patch.object(api_call, "Th2forecastClient", return_value=client), client


class TestNeverThePlatformDatabase:
    def test_sql_without_an_agent_connection_is_refused(self, no_connection):
        with patch("apowerb.tools_store.portfolio.database.tool_run_sql") as run_sql, patch(
            "apowerb.tools_store.portfolio.database._get_connection"
        ) as get_conn, patch.object(api_call, "tool_run_sql", create=True) as bound_run_sql:
            result = api_call.tool_thaink2_forecast(
                date_var="day", target_var="amount", horizon=3, sql=SQL,
            )
        assert result["status"] == "error"
        assert "connexion" in result["message"].lower()
        assert run_sql.call_count == 0
        assert bound_run_sql.call_count == 0
        assert get_conn.call_count == 0

    def test_agent_lookup_failure_is_an_error_not_an_exception(self, monkeypatch):
        def broken(owner=None):
            raise ValueError("invalid literal for int() with base 10: 'abc'")

        monkeypatch.setattr(bi_datasets, "_agent_db_connection", broken)
        result = api_call.tool_thaink2_forecast(date_var="day", target_var="amount", horizon=3, sql=SQL)
        assert result["status"] == "error"
        assert "connexion" in result["message"].lower()

    def test_api_call_no_longer_imports_the_module_run_sql(self):
        assert not hasattr(api_call, "tool_run_sql")


class TestForecastFromTheAgentConnection:
    def test_rows_come_from_the_owner_scoped_executor(self, connection, executor):
        ctx, client = _forecast_client()
        with ctx:
            result = api_call.tool_thaink2_forecast(
                date_var="day", target_var="amount", horizon=3, sql=SQL, group_var="store",
            )
        assert result["status"] == "success", result
        args, kwargs = executor.call_args
        assert (args[0] if args else kwargs["tool_config_id"]) == CONFIG_ID
        assert kwargs["owner_id"] == OWNER
        assert kwargs["max_rows"] > MAX_DATA_ROWS  # one extra row detects truncation
        source = executor.return_value.run.await_args.args[0]
        assert source.query == SQL and source.connection_config_id == CONFIG_ID

    def test_database_types_are_sent_as_json(self, connection, executor):
        ctx, client = _forecast_client()
        with ctx:
            api_call.tool_thaink2_forecast(date_var="day", target_var="amount", horizon=3, sql=SQL)
        sent = client.forecast.call_args.args[0]["data"]
        json.dumps(sent)  # raises on date / Decimal
        assert sent[0] == {"day": "2024-01-01", "store": "Nord", "amount": 90.5}
        assert sent[1]["day"] == "2024-02-01"

    def test_unknown_column_is_refused_before_the_forecast(self, connection, executor):
        ctx, client = _forecast_client()
        with ctx:
            result = api_call.tool_thaink2_forecast(date_var="day", target_var="revenue", horizon=3, sql=SQL)
        assert result["status"] == "error"
        assert "revenue" in result["message"]
        assert client.forecast.call_count == 0

    def test_too_many_rows_is_refused(self, connection, executor):
        executor.return_value.run = AsyncMock(
            return_value=[{"day": "2024-01-01", "amount": 1}] * (MAX_DATA_ROWS + 1)
        )
        ctx, client = _forecast_client()
        with ctx:
            result = api_call.tool_thaink2_forecast(date_var="day", target_var="amount", horizon=3, sql=SQL)
        assert result["status"] == "error"
        assert str(MAX_DATA_ROWS) in result["message"]
        assert client.forecast.call_count == 0

    def test_query_error_is_reported(self, connection, executor):
        executor.return_value.run = AsyncMock(side_effect=RuntimeError("PostgreSQL query error: relation \"x\" does not exist"))
        result = api_call.tool_thaink2_forecast(date_var="day", target_var="amount", horizon=3, sql=SQL)
        assert result["status"] == "error"
        assert "does not exist" in result["message"]


class TestDescribeSql:
    def test_types_and_frequency_are_inferred_from_database_values(self, connection, executor):
        result = bi_datasets.tool_describe_sql(SQL)
        assert result["success"] is True, result
        cols = {c["name"]: c for c in result["columns"]}
        assert cols["day"]["type"] == "date"
        assert cols["day"]["suggested_frequency"] == "month"
        assert cols["amount"]["type"] == "number"
        assert cols["store"]["type"] == "text"
        json.dumps(result)

    def test_without_connection(self, no_connection):
        result = bi_datasets.tool_describe_sql(SQL)
        assert result["success"] is False
        assert "connexion" in result["error"].lower()


@pytest.fixture
def chart_store(monkeypatch):
    @contextlib.asynccontextmanager
    async def no_db():
        yield None

    monkeypatch.setattr(bi, "_get_session", no_db)
    store = InMemoryChartStore()
    monkeypatch.setattr(db_stores, "DatabaseChartStore", lambda db, owner=None: store)
    return store


class TestForecastChartFromSql:
    def test_chart_reads_the_database_through_the_agent_connection(self, connection, executor, chart_store):
        ctx, _ = _forecast_client()
        with ctx:
            result = bi.tool_create_forecast_chart(
                date_var="day", target_var="amount", horizon=3, title="Ventes prévues",
                sql=SQL, group_var="store",
            )
        assert result["success"] is True, result
        import asyncio
        charts = asyncio.run(chart_store.list_all())
        chart = charts[0]
        assert chart.chart_type == ChartType.FORECAST
        assert chart.source.source_type == SourceType.DATABASE
        assert chart.source.query == SQL
        assert chart.source.connection_config_id == CONFIG_ID
        assert chart.source.limit == MAX_DATA_ROWS

    def test_exactly_one_source(self, connection, executor, chart_store):
        both = bi.tool_create_forecast_chart(
            date_var="day", target_var="amount", horizon=3, title="x", sql=SQL, dataset_id="ds1",
        )
        none = bi.tool_create_forecast_chart(date_var="day", target_var="amount", horizon=3, title="x")
        assert both["success"] is False and none["success"] is False

    def test_no_connection_creates_nothing(self, no_connection, chart_store):
        import asyncio
        result = bi.tool_create_forecast_chart(
            date_var="day", target_var="amount", horizon=3, title="x", sql=SQL,
        )
        assert result["success"] is False
        assert asyncio.run(chart_store.list_all()) == []


class TestExecutorRowCap:
    async def test_max_rows_raises_the_default_cap(self):
        from apowerb.bi.data.db_executor import MAX_ROWS, DatabaseQueryExecutor

        ex = DatabaseQueryExecutor(CONFIG_ID, owner_id=OWNER, max_rows=MAX_DATA_ROWS + 1)
        ex._get_config = lambda: {"tool_config_params": {}}
        ex._detect_db_type = lambda cfg: "postgresql"
        ex._run_postgres = AsyncMock(return_value=[])
        await ex.run(DataSource(query=SQL, connection_config_id=CONFIG_ID, limit=None))
        assert ex._run_postgres.await_args.args[1] == MAX_DATA_ROWS + 1

        default = DatabaseQueryExecutor(CONFIG_ID, owner_id=OWNER)
        default._get_config = ex._get_config
        default._detect_db_type = ex._detect_db_type
        default._run_postgres = AsyncMock(return_value=[])
        await default.run(DataSource(query=SQL, connection_config_id=CONFIG_ID, limit=100_000))
        assert default._run_postgres.await_args.args[1] == MAX_ROWS


class TestForecastChartsRenderWithTheForecastCap:
    async def test_service_passes_the_forecast_cap_for_forecast_charts(self):
        from apowerb.bi.charts.core import Chart
        from apowerb.bi.data.schema import DataRequest
        from apowerb.bi.data.service import ChartDataService

        def chart(chart_type):
            return Chart.create(
                name=f"c-{chart_type.value}", title="c", chart_type=chart_type,
                source=DataSource(query=SQL, connection_config_id=CONFIG_ID, limit=MAX_DATA_ROWS),
                organization_id="example.com", created_by=OWNER,
            )

        caps = {}
        for ct in (ChartType.FORECAST, ChartType.LINE):
            c = chart(ct)
            charts = MagicMock()
            charts.get = AsyncMock(return_value=c)
            with patch("apowerb.bi.data.service.DatabaseQueryExecutor") as cls:
                cls.return_value.run = AsyncMock(return_value=[])
                try:
                    await ChartDataService(charts).fetch(c.id, DataRequest(), user_id=OWNER)
                except Exception:
                    pass
                caps[ct] = cls.call_args.kwargs.get("max_rows")
        assert caps[ChartType.FORECAST] == MAX_DATA_ROWS
        assert caps[ChartType.LINE] is None

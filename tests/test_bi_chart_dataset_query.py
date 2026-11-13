"""Un agent agrège un jeu de données importé avec ``tool_create_chart``.

Reproduit sur agent-dev le 21/10/2026 (agent « Analyste Ventes », dataset
``online_store_orders.csv``) : trois runs sur trois, l'agent n'obtient pas
les ventes mensuelles. ``tool_create_chart`` n'avait aucun moyen de viser un
dataset : une requête SQL partait vers la connexion PostgreSQL de l'agent,
où la table n'existe pas, et l'erreur renvoyée l'orientait vers
``tool_text_to_sql``, un outil qu'il n'a pas et qui ne connaît pas les
datasets. Une source ``csv://<id>`` ne rendait que les lignes brutes : le
service des graphiques n'applique ni ``group_by`` ni ``aggregation``.
"""

from __future__ import annotations

import contextlib

import pytest

import apowerb.bi.db_stores as db_stores
import apowerb.tools_store.portfolio.business_intelligence as bi
from apowerb.bi.charts.service import InMemoryChartStore
from apowerb.bi.data import db_executor
from apowerb.tools_store.portfolio import bi_datasets

OWNER = "owner@example.com"
DATASET_ID = "11111111-2222-3333-4444-555555555555"

MONTHLY_SQL = (
    "SELECT strftime(date_trunc('month', order_date), '%Y-%m') AS month, "
    "SUM(order_amount) AS revenue FROM dataset GROUP BY 1 ORDER BY 1"
)


def _orders(dates):
    amounts = [100.0, 50.5, 20.0, 30.0]
    return [
        {"order_date": d, "order_id": i, "order_amount": a, "channel": "web"}
        for i, (d, a) in enumerate(zip(dates, amounts))
    ]


@pytest.fixture
def dataset(monkeypatch):
    """Le dataset possédé par OWNER, lu comme le fait la prévision."""
    loaded = {"rows": _orders(["2026-01-05", "2026-01-20", "2026-02-03", "2026-02-28"])}

    async def load(dataset_id, owner, limit):
        if dataset_id != DATASET_ID or owner != OWNER:
            return {"success": False, "error": bi_datasets._NOT_FOUND_MESSAGE}
        rows = loaded["rows"]
        return {"success": True, "rows": rows, "truncated": False,
                "columns": list(rows[0]), "name": "online_store_orders.csv"}

    monkeypatch.setattr(bi_datasets, "_load_owned_dataset_rows", load)
    return loaded


@pytest.fixture
def chart_env(monkeypatch):
    """Graphiques et datasets en mémoire ; aucune base PostgreSQL."""
    charts = InMemoryChartStore()
    stored = {}

    @contextlib.asynccontextmanager
    async def no_db():
        yield None

    async def store_rows(rows, name, owner_email, organization_id, project_id, origin):
        stored.update(rows=rows, name=name, owner=owner_email)
        return "aggregated-dataset-id"

    monkeypatch.setattr(bi, "_get_session", no_db)
    monkeypatch.setattr(db_stores, "DatabaseChartStore", lambda db, owner=None: charts)
    monkeypatch.setattr(bi, "_async_store_rows_dataset", store_rows)
    return charts, stored


def _create(query, **dataset):
    return bi._async_create_chart(
        name="ventes_mensuelles", title="Ventes mensuelles", chart_type="bar",
        query=query, organization_id="org1", project_id="p", description="",
        refresh_interval=0, time_field="", group_by="", aggregation="",
        connection_config_id="", config="", owner_email=OWNER, **dataset,
    )


class TestAggregateADataset:
    @pytest.mark.asyncio
    async def test_monthly_sum_of_order_amount(self, dataset, chart_env):
        charts, stored = chart_env

        result = await _create(MONTHLY_SQL, dataset_id=DATASET_ID)

        assert result["success"] is True, result
        expected = [{"month": "2026-01", "revenue": 150.5}, {"month": "2026-02", "revenue": 50.0}]
        # The agent gets the figures back to quote them in its answer.
        assert result["data"] == expected
        assert stored["rows"] == expected and stored["owner"] == OWNER
        chart = await charts.get(result["chart_id"])
        assert chart.source.query == "csv://aggregated-dataset-id"

    @pytest.mark.asyncio
    async def test_day_first_dates_are_read_as_dates(self, dataset, chart_env):
        dataset["rows"] = _orders(["05/01/2026", "20/01/2026", "03/02/2026", "28/02/2026"])

        result = await _create(MONTHLY_SQL, dataset_id=DATASET_ID)

        assert result["success"] is True, result
        assert [r["month"] for r in result["data"]] == ["2026-01", "2026-02"]

    @pytest.mark.asyncio
    async def test_a_partly_unreadable_date_column_stays_text(self, dataset, chart_env):
        """Turning it into dates would NULL the odd value and drop its amount."""
        # 9 readable dates out of 10: enough for tool_describe_dataset's "date".
        dates = [f"2026-01-{d:02d}" for d in range(1, 10)] + ["début mars"]
        dataset["rows"] = [{"order_date": d, "order_amount": 10.0} for d in dates]

        result = await _create(
            "SELECT COUNT(*) AS n, SUM(order_amount) AS total FROM dataset "
            "WHERE order_date IS NOT NULL", dataset_id=DATASET_ID,
        )

        assert result["data"] == [{"n": 10, "total": 100.0}]

    @pytest.mark.asyncio
    async def test_a_database_connection_with_a_dataset_is_refused(self, dataset, chart_env):
        result = await bi._async_create_chart(
            name="v", title="V", chart_type="bar", query=MONTHLY_SQL,
            organization_id="org1", project_id="p", description="",
            refresh_interval=0, time_field="", group_by="", aggregation="",
            connection_config_id="tool_config318", config="", owner_email=OWNER,
            dataset_id=DATASET_ID,
        )

        assert result["success"] is False
        assert not chart_env[1]

    @pytest.mark.asyncio
    async def test_never_probes_the_agent_database(self, dataset, chart_env, monkeypatch):
        def no_db_connection(*_a, **_k):
            raise AssertionError("a dataset query must not go to the agent's database")

        monkeypatch.setattr(bi, "_agent_db_connection", no_db_connection)

        result = await _create(MONTHLY_SQL, dataset_id=DATASET_ID)

        assert result["success"] is True, result

    @pytest.mark.asyncio
    async def test_wrong_table_name_says_to_write_from_dataset(self, dataset, chart_env):
        _charts, stored = chart_env

        result = await _create(
            "SELECT SUM(order_amount) FROM online_store_orders", dataset_id=DATASET_ID,
        )

        assert result["success"] is False
        assert "FROM dataset" in result["error"]
        assert "order_date (date)" in result["error"]
        assert not stored

    @pytest.mark.asyncio
    async def test_a_text_date_column_error_says_how_to_cast(self, dataset, chart_env):
        """Timestamps are not read as dates: the agent must learn to convert them."""
        dataset["rows"] = _orders(["2026-01-05 10:00:00", "2026-01-20 11:00:00",
                                   "2026-02-03 12:00:00", "2026-02-28 13:00:00"])

        failed = await _create(MONTHLY_SQL, dataset_id=DATASET_ID)

        assert failed["success"] is False
        assert "order_date (texte)" in failed["error"]
        assert "CAST(col AS DATE)" in failed["error"]
        fixed = await _create(MONTHLY_SQL.replace("order_date", "CAST(order_date AS DATE)"),
                              dataset_id=DATASET_ID)
        assert [r["month"] for r in fixed["data"]] == ["2026-01", "2026-02"]

    @pytest.mark.asyncio
    async def test_a_write_statement_is_refused(self, dataset, chart_env):
        result = await _create("DROP TABLE dataset", dataset_id=DATASET_ID)

        assert result["success"] is False
        assert not chart_env[1]

    @pytest.mark.asyncio
    async def test_someone_elses_dataset_is_not_found(self, dataset, chart_env):
        result = await _create(MONTHLY_SQL, dataset_id="99999999-0000-0000-0000-000000000000")

        assert result == {"success": False, "error": bi_datasets._NOT_FOUND_MESSAGE}

    @pytest.mark.asyncio
    async def test_a_truncated_dataset_is_refused(self, dataset, chart_env, monkeypatch):
        async def too_big(dataset_id, owner, limit):
            return {"success": True, "rows": _orders(["2026-01-05"]), "truncated": True,
                    "columns": ["order_date"], "name": "big.csv"}

        monkeypatch.setattr(bi_datasets, "_load_owned_dataset_rows", too_big)

        result = await _create(MONTHLY_SQL, dataset_id=DATASET_ID)

        assert result["success"] is False
        assert not chart_env[1]


class TestDatabaseErrorPointsToTheDataset:
    @pytest.mark.asyncio
    async def test_unknown_table_error_tells_how_to_query_a_dataset(self, chart_env, monkeypatch):
        """The live failure: SQL over the dataset name, probed on Postgres."""
        monkeypatch.setattr(bi, "_agent_db_connection", lambda: ("tool_config318", OWNER))

        async def failing_run(self, source):
            raise RuntimeError('Query error: relation "online_store_orders" does not exist')

        monkeypatch.setattr(db_executor.DatabaseQueryExecutor, "run", failing_run)

        result = await _create("SELECT SUM(order_amount) FROM online_store_orders")

        assert result["success"] is False
        assert "dataset_id" in result["error"]
        assert "FROM dataset" in result["error"]


def test_the_model_sees_dataset_id_and_how_to_use_it():
    """What the LLM reads: the parameter and the table name, from the docstring."""
    from google.adk.tools import FunctionTool

    declaration = FunctionTool(bi.tool_create_chart)._get_declaration()
    schema = declaration.parameters_json_schema or declaration.parameters.model_dump()

    assert "dataset_id" in schema["properties"]
    assert "FROM dataset" in declaration.description

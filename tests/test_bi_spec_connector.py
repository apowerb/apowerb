"""Dashboard spec v2: connector sources (any chart data source type) and the
forecast visual.

A connector source carries the shape of a chart ``DataSource`` and is read
with the dashboard owner's rights through the same executors as a chart.
The forecast visual returns the filtered series rows; the forecast itself is
computed by the UI widget, as for a forecast chart.
"""

from __future__ import annotations

import contextlib
from typing import Any
from unittest.mock import patch

import pytest

from apowerb.bi.data import service as data_service
from apowerb.bi.spec import engine, sources
from apowerb.bi.spec.models import DashboardDataRequest, FilterSpec, ForecastVisualSpec, SourceSpec, VisualSpec
from apowerb.bi.spec.validate import validate_spec
from tests.helpers.bi_spec_data import OWNER, build_dashboard, dataset_source, weekly_sales


@pytest.fixture(autouse=True)
def _fresh_caches():
    engine.clear_caches()
    yield
    engine.clear_caches()


class RecordingExecutor:
    """Stands in for a chart executor: records who reads what."""

    calls: list[dict[str, Any]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.args = args

    async def run(self, source):
        RecordingExecutor.calls.append({"args": self.args, "kwargs": self.kwargs, "source": source})
        return [{"Mois": "2026-01-01", "Ventes": 10}, {"Mois": "2026-02-01", "Ventes": 12}]


@pytest.fixture
def recorder():
    RecordingExecutor.calls = []
    return RecordingExecutor


def connector(name: str = "ventes", **kw: Any) -> SourceSpec:
    return SourceSpec(name=name, kind="connector", **kw)


class TestConnectorSource:
    async def test_onedrive_file_is_read_with_the_dashboard_owner_rights(self, recorder):
        spec = connector(source_type="onedrive_excel", source_options={"item_id": "file-1"})

        @contextlib.asynccontextmanager
        async def fake_session():
            yield "db-session"

        with patch.object(data_service, "OnedriveExcelQueryExecutor", recorder), \
                patch.object(sources, "_connector_session", fake_session):
            df = await sources.load_source(spec, OWNER)

        assert list(df.columns) == ["mois", "ventes"]
        assert df["ventes"].tolist() == [10, 12]
        (call,) = recorder.calls
        assert call["kwargs"]["owner_id"] == OWNER
        assert call["kwargs"]["db_session"] == "db-session"
        assert call["source"].source_type.value == "onedrive_excel"
        assert call["source"].source_options == {"item_id": "file-1"}

    async def test_database_query_runs_on_the_owner_connection(self, recorder):
        spec = connector(source_type="database", connection_config_id="cfg-1", query="SELECT mois, ventes FROM t")
        with patch.object(data_service, "DatabaseQueryExecutor", recorder):
            await sources.load_source(spec, OWNER)

        (call,) = recorder.calls
        assert call["args"][0] == "cfg-1"
        assert call["kwargs"]["owner_id"] == OWNER
        assert call["source"].query == "SELECT mois, ventes FROM t"

    async def test_agent_source_runs_as_the_owner(self, recorder):
        spec = connector(source_type="agent", source_options={"agent_ids": [3]})
        with patch.object(data_service, "AgentQueryExecutor", recorder):
            await sources.load_source(spec, OWNER)

        (call,) = recorder.calls
        assert call["args"][0] == OWNER

    async def test_executor_error_rows_become_a_source_error(self):
        class Failing(RecordingExecutor):
            async def run(self, source):
                return [{"error": "Missing connection_config_id for Google Drive source"}]

        spec = connector(source_type="google_drive", connection_config_id="cfg-2", query="sheet")
        with patch.object(data_service, "GoogleDriveQueryExecutor", Failing):
            with pytest.raises(Exception, match="Missing connection_config_id"):
                await sources.load_source(spec, OWNER)


class TestConnectorValidation:
    def errors_for(self, source: SourceSpec) -> list[str]:
        return validate_spec(build_dashboard({}, source=source))

    def test_database_needs_a_connection_and_a_select(self):
        errors = self.errors_for(connector(source_type="database"))
        assert any("connection_config_id manquant" in e for e in errors)
        assert any("requête SQL manquante" in e for e in errors)

    def test_onedrive_needs_a_file(self):
        assert any("fichier OneDrive" in e for e in self.errors_for(connector(source_type="onedrive_excel")))

    def test_agent_needs_an_agent(self):
        assert any("agent" in e for e in self.errors_for(connector(source_type="agent")))

    def test_a_csv_file_is_a_dataset_source(self):
        assert any("kind=dataset" in e for e in self.errors_for(connector(source_type="csv", query="csv://x")))

    def test_source_type_is_required(self):
        assert any("source_type manquant" in e for e in self.errors_for(connector()))

    def test_valid_onedrive_source_has_no_error(self):
        source = connector(source_type="onedrive_excel", source_options={"item_path": "/Ventes.xlsx"})
        assert validate_spec(build_dashboard({}, source=source)) == []


def forecast_visual(**kw: Any) -> VisualSpec:
    cfg = {"date_var": "semaine", "target_var": "ventes", "group_var": "famille", "horizon": 8, **kw}
    return VisualSpec(type="forecast", source="ventes", title="Prévision", forecast=ForecastVisualSpec(**cfg))


async def compute(dashboard, **filters):
    async def fake_load(dataset_id, owner, sheet=None):
        return weekly_sales(weeks=20, families=["Alpha", "Bravo"])

    with patch.object(sources, "load_dataset", fake_load):
        return await engine.compute_dashboard(dashboard, DashboardDataRequest(filters=filters), owner=OWNER)


class TestForecastVisual:
    async def test_returns_the_series_rows_sorted_by_group_then_date(self):
        result = await compute(build_dashboard({"f": forecast_visual()}, source=dataset_source(), filters=[]))
        comp = result.components["f"]
        assert comp.error is None, comp.error
        assert comp.type == "forecast"
        assert len(comp.rows) == 40
        assert set(comp.rows[0]) == {"semaine", "ventes", "famille"}
        alpha = [r["semaine"] for r in comp.rows if r["famille"] == "Alpha"]
        assert alpha == sorted(alpha) and len(alpha) == 20
        assert [r["famille"] for r in comp.rows[:20]] == ["Alpha"] * 20

    async def test_dashboard_filters_apply_to_the_series(self):
        flt = [FilterSpec(id="famille", label="Famille", field="famille", type="select")]
        result = await compute(build_dashboard({"f": forecast_visual()}, source=dataset_source(), filters=flt), famille="Bravo")
        rows = result.components["f"].rows
        assert len(rows) == 20 and {r["famille"] for r in rows} == {"Bravo"}

    async def test_widget_options_are_kept_in_the_spec(self):
        visual = forecast_visual(models=["ets"], preprocessing={"outliers": True})
        dumped = visual.model_dump()["forecast"]
        assert dumped["models"] == ["ets"] and dumped["preprocessing"] == {"outliers": True}

    async def test_unknown_field_is_reported_on_the_visual(self):
        dashboard = build_dashboard({"f": forecast_visual(target_var="inconnue")}, source=dataset_source(), filters=[])
        result = await compute(dashboard)
        assert "inconnue" in result.components["f"].error

    def test_forecast_block_is_required(self):
        visual = VisualSpec(type="forecast", source="ventes")
        errors = validate_spec(build_dashboard({"f": visual}, source=dataset_source(), filters=[]))
        assert any("`forecast` manquant" in e for e in errors)

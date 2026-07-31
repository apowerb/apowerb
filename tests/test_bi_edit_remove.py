"""Unit tests for the chart edit/remove BI tools (tool_update_chart,
tool_remove_chart_from_dashboard).

Exercises the exact ChartService / DashboardService operations the sync tool
wrappers orchestrate, using the in-memory stores — no DB / .env required.
"""

import contextlib

import pytest

import apowerb.bi.db_stores as db_stores
import apowerb.tools_store.portfolio.business_intelligence as bi
from apowerb.bi.charts.core import ChartType
from apowerb.bi.data.service import ChartDataService
from apowerb.bi.charts.schemas import (
    ChartCreateRequest,
    ChartUpdateRequest,
    DataSourceSchema,
)
from apowerb.bi.charts.service import ChartService, InMemoryChartStore
from apowerb.bi.dashboards.core import ComponentType
from apowerb.bi.dashboards.schema import (
    AddComponentRequest,
    ChartWidgetSchema,
    ComponentCreateSchema,
    DashboardCreateRequest,
    GridPositionSchema,
)
from apowerb.bi.dashboards.service import DashboardService, InMemoryDashboardStore

OWNER = "test@example.com"


@pytest.fixture
def chart_service():
    return ChartService(InMemoryChartStore())


@pytest.fixture
def dashboard_service():
    return DashboardService(InMemoryDashboardStore())


async def _make_chart(svc, name="c1", title="C1", ctype=ChartType.BAR, query="SELECT a"):
    req = ChartCreateRequest(
        name=name,
        title=title,
        chart_type=ctype,
        source=DataSourceSchema(query=query),
        organization_id="org1",
    )
    return await svc.create(req, created_by=OWNER)


async def _component_ids_for_chart(dashboard, chart_id):
    """Same resolution tool_remove_chart_from_dashboard performs."""
    return [
        c.id
        for c in dashboard.components
        if c.component_type == ComponentType.CHART
        and c.chart is not None
        and c.chart.chart_id == chart_id
    ]


class TestUpdateChart:
    @pytest.mark.asyncio
    async def test_update_title_and_type(self, chart_service):
        chart = await _make_chart(chart_service, ctype=ChartType.BAR)
        updated = await chart_service.update(
            chart.id,
            ChartUpdateRequest(title="New Title", chart_type=ChartType.LINE),
        )
        assert updated.title == "New Title"
        assert updated.chart_type == ChartType.LINE

    @pytest.mark.asyncio
    async def test_merge_source_query_preserves_chart(self, chart_service):
        chart = await _make_chart(chart_service, query="SELECT a")
        # Mirror the tool: merge the new query onto the existing source.
        existing = chart.source.model_dump()
        existing["query"] = "SELECT b"
        updated = await chart_service.update(
            chart.id, ChartUpdateRequest(source=DataSourceSchema(**existing))
        )
        assert updated.source.query == "SELECT b"
        assert updated.id == chart.id


class TestRemoveChartFromDashboard:
    @pytest.mark.asyncio
    async def test_detaches_widget_keeps_chart(self, chart_service, dashboard_service):
        chart = await _make_chart(chart_service)
        dashboard = await dashboard_service.create(
            DashboardCreateRequest(title="Board"), created_by=OWNER
        )
        dashboard = await dashboard_service.add_component(
            dashboard.id,
            AddComponentRequest(
                component=ComponentCreateSchema(
                    component_type="chart",
                    position=GridPositionSchema(row=0, col=0, width=6, height=4),
                    chart=ChartWidgetSchema(chart_id=chart.id),
                )
            ),
        )
        assert len(dashboard.components) == 1

        comp_ids = await _component_ids_for_chart(dashboard, chart.id)
        assert len(comp_ids) == 1

        for cid in comp_ids:
            dashboard = await dashboard_service.remove_component(dashboard.id, cid)

        # Widget gone from the dashboard…
        assert len(dashboard.components) == 0
        # …but the chart object itself is preserved.
        assert (await chart_service.get(chart.id)).id == chart.id

    @pytest.mark.asyncio
    async def test_removes_only_target_chart(self, chart_service, dashboard_service):
        c1 = await _make_chart(chart_service, name="c1", title="C1")
        c2 = await _make_chart(chart_service, name="c2", title="C2")
        dashboard = await dashboard_service.create(
            DashboardCreateRequest(title="Board"), created_by=OWNER
        )
        for c in (c1, c2):
            dashboard = await dashboard_service.add_component(
                dashboard.id,
                AddComponentRequest(
                    component=ComponentCreateSchema(
                        component_type="chart",
                        position=GridPositionSchema(row=0, col=0, width=6, height=4),
                        chart=ChartWidgetSchema(chart_id=c.id),
                    )
                ),
            )
        for cid in await _component_ids_for_chart(dashboard, c1.id):
            dashboard = await dashboard_service.remove_component(dashboard.id, cid)

        remaining = [c.chart.chart_id for c in dashboard.components]
        assert remaining == [c2.id]

    @pytest.mark.asyncio
    async def test_chart_not_on_dashboard_yields_no_match(
        self, chart_service, dashboard_service
    ):
        dashboard = await dashboard_service.create(
            DashboardCreateRequest(title="Empty Board"), created_by=OWNER
        )
        comp_ids = await _component_ids_for_chart(dashboard, "not-here")
        assert comp_ids == []


# ---------------------------------------------------------------------------
# Tool wrappers — exercise the REAL sync tools + _async impls end-to-end,
# with _get_session / Database*Store / _resolve_chart_ref swapped for in-memory
# (so the actual tool code, not just the service, is covered).
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _fake_session():
    yield None


async def _passthrough_resolve(chart_ref, store):
    return chart_ref, None


class TestToolWrappers:
    @pytest.mark.asyncio
    async def test_tool_update_chart_changes_title_and_type(self, monkeypatch):
        store = InMemoryChartStore()
        chart = await _make_chart(ChartService(store), ctype=ChartType.BAR)

        monkeypatch.setattr(bi, "_get_session", _fake_session)
        monkeypatch.setattr(db_stores, "DatabaseChartStore", lambda db, owner=None: store)
        monkeypatch.setattr(bi, "_resolve_chart_ref", _passthrough_resolve)
        monkeypatch.setattr(
            ChartDataService, "invalidate_cache", staticmethod(lambda cid: None)
        )
        monkeypatch.setenv("AGENT_OWNER", OWNER)

        res = bi.tool_update_chart(chart_id=chart.id, title="New T", chart_type="line")
        assert res["success"] is True
        assert res["title"] == "New T"
        assert res["chart_type"] == "line"
        assert (await ChartService(store).get(chart.id)).chart_type == ChartType.LINE

    @pytest.mark.asyncio
    async def test_tool_update_chart_invalid_type_is_reported(self, monkeypatch):
        store = InMemoryChartStore()
        chart = await _make_chart(ChartService(store))

        monkeypatch.setattr(bi, "_get_session", _fake_session)
        monkeypatch.setattr(db_stores, "DatabaseChartStore", lambda db, owner=None: store)
        monkeypatch.setattr(bi, "_resolve_chart_ref", _passthrough_resolve)
        monkeypatch.setenv("AGENT_OWNER", OWNER)

        res = bi.tool_update_chart(chart_id=chart.id, chart_type="banana")
        assert res["success"] is False
        assert "Invalid update" in res["error"]

    @pytest.mark.asyncio
    async def test_tool_remove_chart_detaches_and_keeps_chart(self, monkeypatch):
        chart_store = InMemoryChartStore()
        chart = await _make_chart(ChartService(chart_store))
        dash_store = InMemoryDashboardStore()
        dsvc = DashboardService(dash_store)
        dashboard = await dsvc.create(
            DashboardCreateRequest(title="B"), created_by=OWNER
        )
        dashboard = await dsvc.add_component(
            dashboard.id,
            AddComponentRequest(
                component=ComponentCreateSchema(
                    component_type="chart",
                    position=GridPositionSchema(row=0, col=0, width=6, height=4),
                    chart=ChartWidgetSchema(chart_id=chart.id),
                )
            ),
        )

        monkeypatch.setattr(bi, "_get_session", _fake_session)
        monkeypatch.setattr(db_stores, "DatabaseChartStore", lambda db, owner=None: chart_store)
        monkeypatch.setattr(db_stores, "DatabaseDashboardStore", lambda db, owner=None: dash_store)
        monkeypatch.setattr(bi, "_resolve_chart_ref", _passthrough_resolve)
        monkeypatch.setenv("AGENT_OWNER", OWNER)
        monkeypatch.setenv("AGENT_DASHBOARD_ID", dashboard.id)

        res = bi.tool_remove_chart_from_dashboard(chart_id=chart.id)
        assert res["success"] is True
        assert res["removed"] == 1
        assert res["component_count"] == 0
        # chart object preserved
        assert (await ChartService(chart_store).get(chart.id)).id == chart.id

    @pytest.mark.asyncio
    async def test_tool_remove_missing_chart_reports_error(self, monkeypatch):
        chart_store = InMemoryChartStore()
        dash_store = InMemoryDashboardStore()
        dsvc = DashboardService(dash_store)
        dashboard = await dsvc.create(
            DashboardCreateRequest(title="Empty"), created_by=OWNER
        )

        monkeypatch.setattr(bi, "_get_session", _fake_session)
        monkeypatch.setattr(db_stores, "DatabaseChartStore", lambda db, owner=None: chart_store)
        monkeypatch.setattr(db_stores, "DatabaseDashboardStore", lambda db, owner=None: dash_store)
        monkeypatch.setattr(bi, "_resolve_chart_ref", _passthrough_resolve)
        monkeypatch.setenv("AGENT_OWNER", OWNER)
        monkeypatch.setenv("AGENT_DASHBOARD_ID", dashboard.id)

        res = bi.tool_remove_chart_from_dashboard(chart_id="ghost")
        assert res["success"] is False
        assert "not on this dashboard" in res["error"]


class TestUpdateKeyValueComponent:
    """update_component edits a key_value tile's content (retour Anis #3)."""

    @pytest.mark.asyncio
    async def test_update_key_value_content(self, dashboard_service):
        from apowerb.bi.dashboards.schema import (
            KeyValueCreateSchema,
            UpdateComponentRequest,
        )

        dashboard = await dashboard_service.create(
            DashboardCreateRequest(title="KPI Board"), created_by=OWNER
        )
        dashboard = await dashboard_service.add_component(
            dashboard.id,
            AddComponentRequest(
                component=ComponentCreateSchema(
                    component_type=ComponentType.KEY_VALUE,
                    position=GridPositionSchema(row=0, col=0, width=4, height=2),
                    key_value=KeyValueCreateSchema(label="Old", value=10, unit="x"),
                )
            ),
        )
        comp_id = dashboard.components[0].id
        before_version = dashboard.version

        updated = await dashboard_service.update_component(
            dashboard.id,
            comp_id,
            UpdateComponentRequest(
                key_value=KeyValueCreateSchema(
                    label="Total runs", value=42, unit="runs", description="today"
                )
            ),
        )
        kv = updated.components[0].key_value
        assert kv.label == "Total runs"
        assert kv.value == 42
        assert kv.unit == "runs"
        assert kv.description == "today"
        assert updated.version == before_version + 1

    @pytest.mark.asyncio
    async def test_update_rejects_chart_component(
        self, dashboard_service, chart_service
    ):
        from apowerb.bi.dashboards.schema import (
            KeyValueCreateSchema,
            UpdateComponentRequest,
        )

        chart = await _make_chart(chart_service)
        dashboard = await dashboard_service.create(
            DashboardCreateRequest(title="Chart Board"), created_by=OWNER
        )
        dashboard = await dashboard_service.add_component(
            dashboard.id,
            AddComponentRequest(
                component=ComponentCreateSchema(
                    component_type=ComponentType.CHART,
                    position=GridPositionSchema(row=0, col=0, width=8, height=4),
                    chart=ChartWidgetSchema(chart_id=chart.id),
                )
            ),
        )
        comp_id = dashboard.components[0].id
        with pytest.raises(ValueError):
            await dashboard_service.update_component(
                dashboard.id,
                comp_id,
                UpdateComponentRequest(
                    key_value=KeyValueCreateSchema(label="x", value=1)
                ),
            )

    @pytest.mark.asyncio
    async def test_update_missing_component_raises(self, dashboard_service):
        from apowerb.bi.dashboards.service import ComponentNotFoundError
        from apowerb.bi.dashboards.schema import (
            KeyValueCreateSchema,
            UpdateComponentRequest,
        )

        dashboard = await dashboard_service.create(
            DashboardCreateRequest(title="Empty"), created_by=OWNER
        )
        with pytest.raises(ComponentNotFoundError):
            await dashboard_service.update_component(
                dashboard.id,
                "nonexistent",
                UpdateComponentRequest(
                    key_value=KeyValueCreateSchema(label="x", value=1)
                ),
            )

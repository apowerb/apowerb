"""Unit tests for BI agent tools (business_intelligence.py).

Tests the async internal implementations using InMemoryChartStore and
InMemoryDashboardStore — no real database connection required.
"""

import pytest

from apowerb.bi.charts.core import Chart, ChartType, DataSource
from apowerb.bi.charts.schemas import ChartCreateRequest, ChartUpdateRequest, DataSourceSchema
from apowerb.bi.charts.service import ChartNotFoundError, ChartService, InMemoryChartStore
from apowerb.bi.dashboards.core import (
    Dashboard,
    DashboardComponent,
    DashboardStatus,
    DashboardVisibility,
    GridPosition,
    KeyValue,
    Trend,
    TrendDirection,
    TrendSentiment,
)
from apowerb.bi.dashboards.schema import (
    AddComponentRequest,
    ChartWidgetSchema,
    ComponentCreateSchema,
    DashboardCreateRequest,
    GridPositionSchema,
    KeyValueCreateSchema,
    TrendSchema,
)
from apowerb.bi.dashboards.service import (
    DashboardNotFoundError,
    DashboardService,
    InMemoryDashboardStore,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def chart_store():
    return InMemoryChartStore()


@pytest.fixture
def chart_service(chart_store):
    return ChartService(chart_store)


@pytest.fixture
def dashboard_store():
    return InMemoryDashboardStore()


@pytest.fixture
def dashboard_service(dashboard_store):
    return DashboardService(dashboard_store)


# ---------------------------------------------------------------------------
# TestToolCreateChart
# ---------------------------------------------------------------------------


class TestToolCreateChart:
    @pytest.mark.asyncio
    async def test_create_bar_chart(self, chart_service):
        req = ChartCreateRequest(
            name="test_bar",
            title="Test Bar Chart",
            chart_type=ChartType.BAR,
            source=DataSourceSchema(query="SELECT * FROM sales"),
            organization_id="org1",
        )
        chart = await chart_service.create(req, created_by="test@example.com")
        assert chart.chart_type == ChartType.BAR
        assert chart.title == "Test Bar Chart"
        assert chart.name == "test_bar"
        assert chart.organization_id == "org1"

    @pytest.mark.asyncio
    async def test_create_stat_kpi_chart(self, chart_service):
        chart = await chart_service.create_kpi(
            name="total_users_kpi",
            title="Total Users",
            query="SELECT count(*) FROM users",
            organization_id="org1",
            created_by="test@example.com",
        )
        assert chart.chart_type == ChartType.STAT
        assert chart.title == "Total Users"
        assert chart.source.aggregation is not None

    @pytest.mark.asyncio
    async def test_create_chart_invalid_type(self):
        with pytest.raises(ValueError):
            ChartCreateRequest(
                name="bad_chart",
                title="Bad Chart",
                chart_type="invalid_type",
                source=DataSourceSchema(query="SELECT 1"),
                organization_id="org1",
            )

    @pytest.mark.asyncio
    async def test_create_chart_returns_chart_id(self, chart_service):
        req = ChartCreateRequest(
            name="id_test",
            title="ID Test Chart",
            chart_type=ChartType.LINE,
            source=DataSourceSchema(query="SELECT * FROM metrics"),
            organization_id="org1",
        )
        chart = await chart_service.create(req, created_by="test@example.com")
        assert chart.id is not None
        assert len(chart.id) > 0

        # Verify we can retrieve it
        fetched = await chart_service.get(chart.id)
        assert fetched.id == chart.id


# ---------------------------------------------------------------------------
# TestToolCreateDashboard
# ---------------------------------------------------------------------------


class TestToolCreateDashboard:
    @pytest.mark.asyncio
    async def test_create_dashboard(self, dashboard_service):
        req = DashboardCreateRequest(
            title="Test Dashboard",
            description="A test dashboard",
        )
        dashboard = await dashboard_service.create(req, created_by="test@example.com")
        assert dashboard.title == "Test Dashboard"
        assert dashboard.description == "A test dashboard"
        assert dashboard.status == DashboardStatus.DRAFT
        assert dashboard.id is not None

    @pytest.mark.asyncio
    async def test_create_dashboard_with_slug(self, dashboard_service):
        req = DashboardCreateRequest(
            title="Sales Overview",
            slug="sales-overview",
        )
        dashboard = await dashboard_service.create(req, created_by="test@example.com")
        assert dashboard.slug == "sales-overview"


# ---------------------------------------------------------------------------
# TestToolAddChartToDashboard
# ---------------------------------------------------------------------------


class TestToolAddChartToDashboard:
    @pytest.mark.asyncio
    async def test_add_chart_to_dashboard(self, chart_service, dashboard_service):
        # Create chart
        req = ChartCreateRequest(
            name="chart_to_add",
            title="Chart to Add",
            chart_type=ChartType.BAR,
            source=DataSourceSchema(query="SELECT * FROM data"),
            organization_id="org1",
        )
        chart = await chart_service.create(req, created_by="test@example.com")

        # Create dashboard
        dash_req = DashboardCreateRequest(title="Dashboard with Chart")
        dashboard = await dashboard_service.create(dash_req, created_by="test@example.com")

        # Add chart to dashboard
        comp_req = AddComponentRequest(
            component=ComponentCreateSchema(
                component_type="chart",
                position=GridPositionSchema(row=0, col=0, width=6, height=4),
                chart=ChartWidgetSchema(chart_id=chart.id),
            )
        )
        updated = await dashboard_service.add_component(dashboard.id, comp_req)
        assert len(updated.components) == 1
        assert updated.components[0].chart.chart_id == chart.id

    @pytest.mark.asyncio
    async def test_add_chart_nonexistent_dashboard(self, dashboard_service):
        comp_req = AddComponentRequest(
            component=ComponentCreateSchema(
                component_type="chart",
                position=GridPositionSchema(row=0, col=0, width=6, height=4),
                chart=ChartWidgetSchema(chart_id="fake-chart-id"),
            )
        )
        with pytest.raises(DashboardNotFoundError):
            await dashboard_service.add_component("nonexistent-id", comp_req)

    @pytest.mark.asyncio
    async def test_add_chart_with_position(self, dashboard_service):
        dash_req = DashboardCreateRequest(title="Positioned Dashboard")
        dashboard = await dashboard_service.create(dash_req, created_by="test@example.com")

        comp_req = AddComponentRequest(
            component=ComponentCreateSchema(
                component_type="chart",
                position=GridPositionSchema(row=2, col=6, width=6, height=4),
                chart=ChartWidgetSchema(chart_id="some-chart-id"),
            )
        )
        updated = await dashboard_service.add_component(dashboard.id, comp_req)
        assert updated.components[0].position.row == 2
        assert updated.components[0].position.col == 6
        assert updated.components[0].position.width == 6


# ---------------------------------------------------------------------------
# TestToolAddKpiToDashboard
# ---------------------------------------------------------------------------


class TestToolAddKpiToDashboard:
    @pytest.mark.asyncio
    async def test_add_kpi(self, dashboard_service):
        dash_req = DashboardCreateRequest(title="KPI Dashboard")
        dashboard = await dashboard_service.create(dash_req, created_by="test@example.com")

        comp_req = AddComponentRequest(
            component=ComponentCreateSchema(
                component_type="key_value",
                position=GridPositionSchema(row=0, col=0, width=3, height=2),
                key_value=KeyValueCreateSchema(
                    label="Total Revenue",
                    value=12450,
                    unit="EUR",
                ),
            )
        )
        updated = await dashboard_service.add_component(dashboard.id, comp_req)
        assert len(updated.components) == 1
        assert updated.components[0].key_value.label == "Total Revenue"
        assert updated.components[0].key_value.value == 12450
        assert updated.components[0].key_value.unit == "EUR"

    @pytest.mark.asyncio
    async def test_add_kpi_with_trend(self, dashboard_service):
        dash_req = DashboardCreateRequest(title="KPI Trend Dashboard")
        dashboard = await dashboard_service.create(dash_req, created_by="test@example.com")

        comp_req = AddComponentRequest(
            component=ComponentCreateSchema(
                component_type="key_value",
                position=GridPositionSchema(row=0, col=0, width=3, height=2),
                key_value=KeyValueCreateSchema(
                    label="Active Users",
                    value=2481,
                    unit="users",
                    trend=TrendSchema(
                        value=12.4,
                        direction=TrendDirection.UP,
                        sentiment=TrendSentiment.POSITIVE,
                    ),
                ),
            )
        )
        updated = await dashboard_service.add_component(dashboard.id, comp_req)
        kpi = updated.components[0].key_value
        assert kpi.trend is not None
        assert kpi.trend.direction == TrendDirection.UP
        assert kpi.trend.sentiment == TrendSentiment.POSITIVE
        assert kpi.trend.value == 12.4


# ---------------------------------------------------------------------------
# TestToolPublishDashboard
# ---------------------------------------------------------------------------


class TestToolPublishDashboard:
    @pytest.mark.asyncio
    async def test_publish_private(self, dashboard_service):
        dash_req = DashboardCreateRequest(title="Private Dashboard")
        dashboard = await dashboard_service.create(dash_req, created_by="test@example.com")

        published = await dashboard_service.publish(
            dashboard.id, visibility=DashboardVisibility.PRIVATE
        )
        assert published.status == DashboardStatus.PUBLISHED
        assert published.visibility == DashboardVisibility.PRIVATE

    @pytest.mark.asyncio
    async def test_publish_organization(self, dashboard_service):
        dash_req = DashboardCreateRequest(title="Org Dashboard")
        dashboard = await dashboard_service.create(dash_req, created_by="test@example.com")

        published = await dashboard_service.publish(
            dashboard.id, visibility=DashboardVisibility.ORGANIZATION
        )
        assert published.status == DashboardStatus.PUBLISHED
        assert published.visibility == DashboardVisibility.ORGANIZATION
        assert published.slug is not None  # Auto-generated from title

    @pytest.mark.asyncio
    async def test_publish_nonexistent(self, dashboard_service):
        with pytest.raises(DashboardNotFoundError):
            await dashboard_service.publish("nonexistent-id")


# ---------------------------------------------------------------------------
# TestToolListDashboards
# ---------------------------------------------------------------------------


class TestToolListDashboards:
    @pytest.mark.asyncio
    async def test_list_empty(self, dashboard_service):
        dashboards, total = await dashboard_service.list()
        assert dashboards == []
        assert total == 0

    @pytest.mark.asyncio
    async def test_list_with_dashboards(self, dashboard_service):
        for i in range(3):
            req = DashboardCreateRequest(title=f"Dashboard {i}")
            await dashboard_service.create(req, created_by="test@example.com")

        dashboards, total = await dashboard_service.list()
        assert total == 3
        assert len(dashboards) == 3

    @pytest.mark.asyncio
    async def test_list_pagination(self, dashboard_service):
        for i in range(5):
            req = DashboardCreateRequest(title=f"Dashboard {i}")
            await dashboard_service.create(req, created_by="test@example.com")

        page1, total = await dashboard_service.list(page=1, page_size=2)
        assert len(page1) == 2
        assert total == 5

        page2, total2 = await dashboard_service.list(page=2, page_size=2)
        assert len(page2) == 2
        assert total2 == 5

        page3, total3 = await dashboard_service.list(page=3, page_size=2)
        assert len(page3) == 1
        assert total3 == 5


# ---------------------------------------------------------------------------
# TestToolUpdateChartData
# ---------------------------------------------------------------------------


class TestToolUpdateChartData:
    @pytest.mark.asyncio
    async def test_update_query(self, chart_service):
        req = ChartCreateRequest(
            name="updatable_chart",
            title="Updatable Chart",
            chart_type=ChartType.BAR,
            source=DataSourceSchema(query="SELECT * FROM old_table"),
            organization_id="org1",
        )
        chart = await chart_service.create(req, created_by="test@example.com")

        update_req = ChartUpdateRequest(
            source=DataSourceSchema(query="SELECT * FROM new_table"),
        )
        updated = await chart_service.update(chart.id, update_req)
        assert updated.source.query == "SELECT * FROM new_table"

    @pytest.mark.asyncio
    async def test_update_nonexistent(self, chart_service):
        update_req = ChartUpdateRequest(
            source=DataSourceSchema(query="SELECT 1"),
        )
        with pytest.raises(ChartNotFoundError):
            await chart_service.update("nonexistent-id", update_req)


# ---------------------------------------------------------------------------
# TestFullWorkflow
# ---------------------------------------------------------------------------


class TestFullWorkflow:
    @pytest.mark.asyncio
    async def test_complete_dashboard_creation(self, chart_service, dashboard_service):
        """Full workflow: create charts -> create dashboard -> add charts -> add KPI -> publish."""

        # Step 1: Create charts
        bar_req = ChartCreateRequest(
            name="monthly_revenue",
            title="Monthly Revenue",
            chart_type=ChartType.BAR,
            source=DataSourceSchema(query="SELECT month, revenue FROM sales"),
            organization_id="org1",
            refresh_interval=60,
        )
        bar_chart = await chart_service.create(bar_req, created_by="test@example.com")

        line_req = ChartCreateRequest(
            name="daily_users",
            title="Daily Active Users",
            chart_type=ChartType.LINE,
            source=DataSourceSchema(query="SELECT date, count FROM users", time_field="date"),
            organization_id="org1",
            refresh_interval=60,
        )
        line_chart = await chart_service.create(line_req, created_by="test@example.com")

        # Step 2: Create dashboard
        dash_req = DashboardCreateRequest(
            title="Sales Overview",
            description="Complete sales dashboard",
            slug="sales-overview",
        )
        dashboard = await dashboard_service.create(dash_req, created_by="test@example.com")
        assert dashboard.status == DashboardStatus.DRAFT

        # Step 3: Add KPI
        kpi_req = AddComponentRequest(
            component=ComponentCreateSchema(
                component_type="key_value",
                position=GridPositionSchema(row=0, col=0, width=3, height=2),
                key_value=KeyValueCreateSchema(
                    label="Total Revenue",
                    value=125000,
                    unit="EUR",
                    trend=TrendSchema(
                        value=15.3,
                        direction=TrendDirection.UP,
                        sentiment=TrendSentiment.POSITIVE,
                    ),
                ),
            )
        )
        dashboard = await dashboard_service.add_component(dashboard.id, kpi_req)
        assert len(dashboard.components) == 1

        # Step 4: Add charts
        chart1_req = AddComponentRequest(
            component=ComponentCreateSchema(
                component_type="chart",
                position=GridPositionSchema(row=2, col=0, width=8, height=4),
                chart=ChartWidgetSchema(chart_id=bar_chart.id),
            )
        )
        dashboard = await dashboard_service.add_component(dashboard.id, chart1_req)
        assert len(dashboard.components) == 2

        chart2_req = AddComponentRequest(
            component=ComponentCreateSchema(
                component_type="chart",
                position=GridPositionSchema(row=2, col=8, width=4, height=4),
                chart=ChartWidgetSchema(chart_id=line_chart.id),
            )
        )
        dashboard = await dashboard_service.add_component(dashboard.id, chart2_req)
        assert len(dashboard.components) == 3

        # Step 5: Publish
        published = await dashboard_service.publish(
            dashboard.id, visibility=DashboardVisibility.ORGANIZATION
        )
        assert published.status == DashboardStatus.PUBLISHED
        assert published.visibility == DashboardVisibility.ORGANIZATION
        assert published.slug == "sales-overview"
        assert len(published.components) == 3
        assert published.version > dashboard.version

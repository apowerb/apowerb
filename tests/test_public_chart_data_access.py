"""Accès à /api/v1/public/charts/{chart_id}/data.

La route exige un utilisateur connecté (401 sinon), puis sert les données
seulement au propriétaire du graphique ou à un lecteur d'un tableau de bord
PUBLIÉ qui l'affiche et dont la visibilité l'admet (même règle que
/dashboards/public/{slug}). Un graphique non visible répond 404, sans
confirmer son existence. Les credentials de la source sont ceux du
propriétaire enregistré (colonne ``owner``), jamais ``created_by``.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apowerb.bi.dashboards.core import DashboardStatus, DashboardVisibility

OWNER = "creator@example.com"
COLLEAGUE = "colleague@example.com"
OUTSIDER = "someone@other.org"


def _user(email):
    user = MagicMock()
    user.email = email
    return user


def _build_app(viewer=None):
    from apowerb.auth.dependencies import get_current_user
    from apowerb.bi.data.router import router as data_router
    from apowerb.helpers.database import get_db

    app = FastAPI()
    app.include_router(data_router, prefix="/api/v1")

    async def fake_db():
        yield AsyncMock()

    app.dependency_overrides[get_db] = fake_db
    if viewer is not None:
        app.dependency_overrides[get_current_user] = lambda: _user(viewer)
    return app


def _fake_data_response():
    from apowerb.bi.charts.core import ChartType
    from apowerb.bi.data.schema import ChartDataResponse, PageMeta

    return ChartDataResponse(
        chart_id="chart1",
        chart_type=ChartType.BAR,
        title="Fake",
        labels=[],
        rows=[],
        pagination=PageMeta(page=1, page_size=50, total=0, has_next=False, has_prev=False),
    )


def _get(app, *, owner=OWNER, on_visible_dashboard=False, created_by=OWNER):
    chart = MagicMock()
    chart.id = "chart1"
    chart.created_by = created_by
    fetch = AsyncMock(return_value=_fake_data_response())
    with patch(
        "apowerb.bi.data.router._stored_chart_owner", AsyncMock(return_value=owner)
    ), patch(
        "apowerb.bi.data.router.chart_on_visible_dashboard",
        AsyncMock(return_value=on_visible_dashboard),
    ), patch("apowerb.bi.charts.service.ChartService") as chart_svc, patch(
        "apowerb.bi.data.service.ChartDataService"
    ) as data_svc:
        chart_svc.return_value.get = AsyncMock(return_value=chart)
        data_svc.return_value.fetch = fetch
        resp = TestClient(app).get("/api/v1/public/charts/chart1/data")
    return resp, fetch


class TestAuthenticationRequired:
    def test_no_token_is_401_and_reads_nothing(self):
        resp, fetch = _get(_build_app(viewer=None), on_visible_dashboard=True)
        assert resp.status_code == 401
        assert fetch.await_count == 0


class TestVisibilityGate:
    def test_owner_reads_their_chart(self):
        resp, fetch = _get(_build_app(viewer=OWNER))
        assert resp.status_code == 200
        assert fetch.await_count == 1

    def test_owner_match_ignores_case(self):
        resp, _ = _get(_build_app(viewer=OWNER.upper()))
        assert resp.status_code == 200

    def test_other_user_on_no_visible_dashboard_gets_404(self):
        resp, fetch = _get(_build_app(viewer=OUTSIDER), on_visible_dashboard=False)
        assert resp.status_code == 404
        assert fetch.await_count == 0

    def test_viewer_of_a_visible_published_dashboard_reads_it(self):
        resp, fetch = _get(_build_app(viewer=OUTSIDER), on_visible_dashboard=True)
        assert resp.status_code == 200
        assert fetch.await_count == 1

    def test_missing_chart_row_gets_404(self):
        resp, fetch = _get(_build_app(viewer=OWNER), owner=None, on_visible_dashboard=True)
        assert resp.status_code == 404
        assert fetch.await_count == 0


class TestCredentialsComeFromTheStoredOwner:
    def test_forged_created_by_does_not_pick_the_credentials(self):
        resp, fetch = _get(
            _build_app(viewer=OUTSIDER), on_visible_dashboard=True, created_by="victim@example.com"
        )
        assert resp.status_code == 200
        assert fetch.await_args.kwargs["user_id"] == OWNER


def _dashboard_config(chart_ids, *, status=DashboardStatus.PUBLISHED,
                      visibility=DashboardVisibility.PUBLIC, created_by=OWNER):
    from apowerb.bi.dashboards.core import ChartWidget, Dashboard, DashboardComponent

    dashboard = Dashboard.create(title="Ventes", slug="ventes", created_by=created_by)
    components = [DashboardComponent(component_type="chart", chart=ChartWidget(chart_id=c)) for c in chart_ids]
    return dashboard.model_copy(update={
        "components": components, "status": status, "visibility": visibility,
    }).model_dump(mode="json")


def _db_returning(configs):
    """Rows of (owner, config); the owner column matches each config's creator."""
    result = MagicMock()
    result.all.return_value = [(c.get("created_by"), c) for c in configs]
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    return db


class TestChartOnVisibleDashboard:
    async def _check(self, configs, viewer, chart_id="chart1"):
        from apowerb.bi.dashboards.access import chart_on_visible_dashboard

        return await chart_on_visible_dashboard(_db_returning(configs), chart_id, viewer)

    async def test_public_published_dashboard_admits_any_user(self):
        assert await self._check([_dashboard_config(["chart1"])], OUTSIDER)

    async def test_chart_absent_from_the_dashboard_is_not_visible(self):
        assert not await self._check([_dashboard_config(["chart2"])], OUTSIDER)

    async def test_draft_dashboard_does_not_grant_access(self):
        assert not await self._check([_dashboard_config(["chart1"], status=DashboardStatus.DRAFT)], OUTSIDER)

    async def test_organization_dashboard_admits_the_same_domain_only(self):
        cfg = [_dashboard_config(["chart1"], visibility=DashboardVisibility.ORGANIZATION)]
        assert await self._check(cfg, COLLEAGUE)
        assert not await self._check(cfg, OUTSIDER)

    async def test_invalid_dashboard_config_grants_nothing(self):
        assert not await self._check([{"status": "published", "components": [{"chart": {"chart_id": "chart1"}}]}], OUTSIDER)


class TestVisibleTo:
    def test_rules(self):
        from apowerb.bi.dashboards.access import visible_to
        from apowerb.bi.dashboards.core import Dashboard

        def dash(visibility, created_by=OWNER):
            return Dashboard.model_validate(_dashboard_config([], visibility=visibility, created_by=created_by))

        assert visible_to(dash(DashboardVisibility.PUBLIC), OUTSIDER)
        # Published dashboards default to public visibility (backwards compat).
        assert visible_to(dash(DashboardVisibility.PRIVATE), OUTSIDER)
        assert visible_to(dash(DashboardVisibility.ORGANIZATION), "Colleague@EXAMPLE.com")
        assert not visible_to(dash(DashboardVisibility.ORGANIZATION), OUTSIDER)
        assert not visible_to(dash(DashboardVisibility.ORGANIZATION, created_by="no-domain"), OUTSIDER)

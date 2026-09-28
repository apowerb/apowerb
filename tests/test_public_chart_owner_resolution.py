"""Tests pour /api/v1/public/charts/{chart_id}/data.

Vérifient que le endpoint public dérive `user_id` du propriétaire enregistré
(colonne ``owner`` de la ligne) pour récupérer les credentials owner-scopés
(sinon la source DB/Drive/Agent casse après le fix cross-tenant G1), et jamais
de ``chart.created_by``, modifiable dans la config. L'accès lui-même est couvert
par tests/test_public_chart_data_access.py.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient


OWNER = "creator@example.com"


def _build_app():
    """Data router only: dummy session, OWNER logged in, stored owner = OWNER."""
    from apowerb.auth.dependencies import get_current_user
    from apowerb.bi.data.router import router as data_router
    from apowerb.helpers.database import get_db

    app = FastAPI()
    app.include_router(data_router, prefix="/api/v1")

    async def fake_db():
        yield AsyncMock()

    app.dependency_overrides[get_db] = fake_db
    app.dependency_overrides[get_current_user] = lambda: MagicMock(email=OWNER)
    return app


def _stored_owner(owner: str | None = OWNER):
    return patch("apowerb.bi.data.router._stored_chart_owner", AsyncMock(return_value=owner))


def _fake_chart(chart_id: str = "chart1", created_by: str | None = OWNER):
    chart = MagicMock()
    chart.id = chart_id
    chart.created_by = created_by
    return chart


def _fake_data_response():
    from apowerb.bi.data.schema import ChartDataResponse, PageMeta
    from apowerb.bi.charts.core import ChartType

    return ChartDataResponse(
        chart_id="chart1",
        chart_type=ChartType.BAR,
        title="Fake",
        labels=[],
        rows=[],
        pagination=PageMeta(page=1, page_size=50, total=0, has_next=False, has_prev=False),
    )


class TestPublicChartOwnerResolution:
    def test_public_endpoint_forwards_stored_owner_as_user_id(self):
        """Le endpoint public passe le propriétaire enregistré à `data_svc.fetch`."""
        app = _build_app()

        fake_chart = _fake_chart(created_by="forged@example.com")
        mock_fetch = AsyncMock(return_value=_fake_data_response())
        mock_get = AsyncMock(return_value=fake_chart)

        with _stored_owner(), patch(
            "apowerb.bi.charts.service.ChartService"
        ) as MockChartSvc, patch(
            "apowerb.bi.data.service.ChartDataService"
        ) as MockDataSvc:
            MockChartSvc.return_value.get = mock_get
            MockDataSvc.return_value.fetch = mock_fetch

            client = TestClient(app)
            resp = client.get("/api/v1/public/charts/chart1/data")

        assert resp.status_code == 200
        assert mock_fetch.await_count == 1
        _, kwargs = mock_fetch.await_args
        assert kwargs.get("user_id") == OWNER

    def test_public_endpoint_404_without_stored_row(self):
        """Pas de ligne enregistrée (graphique supprimé ou inconnu) → 404, rien lu."""
        app = _build_app()
        mock_fetch = AsyncMock(return_value=_fake_data_response())

        with _stored_owner(None), patch(
            "apowerb.bi.data.service.ChartDataService"
        ) as MockDataSvc:
            MockDataSvc.return_value.fetch = mock_fetch
            resp = TestClient(app).get("/api/v1/public/charts/chart1/data")

        assert resp.status_code == 404
        assert mock_fetch.await_count == 0

    def test_public_endpoint_returns_404_when_chart_missing(self):
        from apowerb.bi.charts.service import ChartNotFoundError

        app = _build_app()

        with _stored_owner(), patch(
            "apowerb.bi.data.service.ChartDataService"
        ) as MockDataSvc:
            MockDataSvc.return_value.fetch = AsyncMock(side_effect=ChartNotFoundError("nope"))

            client = TestClient(app)
            resp = client.get("/api/v1/public/charts/missing/data")

        assert resp.status_code == 404

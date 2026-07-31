"""Tests pour /api/v1/public/charts/{chart_id}/data.

Vérifient que le endpoint public dérive `user_id` depuis `chart.created_by`
pour permettre la récupération des credentials owner-scopés
(sinon la source DB/Drive/Agent casse après le fix cross-tenant G1).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


OWNER = "creator@example.com"


def _build_app():
    """Include only the data router with get_db overridden to a dummy session."""
    from apowerb.bi.data.router import router as data_router
    from apowerb.helpers.database import get_db

    app = FastAPI()
    app.include_router(data_router, prefix="/api/v1")

    async def fake_db():
        yield AsyncMock()

    app.dependency_overrides[get_db] = fake_db
    return app


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
    def test_public_endpoint_forwards_created_by_as_user_id(self):
        """Le endpoint public doit passer `chart.created_by` à `data_svc.fetch`."""
        app = _build_app()

        fake_chart = _fake_chart(created_by=OWNER)
        mock_fetch = AsyncMock(return_value=_fake_data_response())
        mock_get = AsyncMock(return_value=fake_chart)

        with patch(
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

    def test_public_endpoint_passes_none_when_created_by_missing(self):
        """Chart legacy sans created_by → user_id=None (fallback, casse si source protégée)."""
        app = _build_app()

        fake_chart = _fake_chart(created_by=None)
        mock_fetch = AsyncMock(return_value=_fake_data_response())
        mock_get = AsyncMock(return_value=fake_chart)

        with patch(
            "apowerb.bi.charts.service.ChartService"
        ) as MockChartSvc, patch(
            "apowerb.bi.data.service.ChartDataService"
        ) as MockDataSvc:
            MockChartSvc.return_value.get = mock_get
            MockDataSvc.return_value.fetch = mock_fetch

            client = TestClient(app)
            resp = client.get("/api/v1/public/charts/chart1/data")

        assert resp.status_code == 200
        _, kwargs = mock_fetch.await_args
        assert kwargs.get("user_id") is None

    def test_public_endpoint_returns_404_when_chart_missing(self):
        from apowerb.bi.charts.service import ChartNotFoundError

        app = _build_app()

        mock_get = AsyncMock(side_effect=ChartNotFoundError("nope"))

        with patch("apowerb.bi.charts.service.ChartService") as MockChartSvc:
            MockChartSvc.return_value.get = mock_get

            client = TestClient(app)
            resp = client.get("/api/v1/public/charts/missing/data")

        assert resp.status_code == 404

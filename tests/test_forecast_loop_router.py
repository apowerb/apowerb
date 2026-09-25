"""Tests de POST /api/v1/forecast avec `chart_id` — boucle fermée (contrat
étape 5 §3) : accès, relais, suivi, instantané. th2forecast et la base sont
tous les deux remplacés ; la logique de calcul du suivi est testée à part
dans tests/test_forecast_tracking.py."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _fake_user(email: str = "alice@example.com", user_id: int = 1):
    u = MagicMock()
    u.email, u.user_id, u.role = email, user_id, "USER"
    return u


def _build_app(*, user_email: str = "alice@example.com"):
    from apowerb.auth.dependencies import get_current_user
    from apowerb.helpers.database import get_db
    from apowerb.routers.forecast import router

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: _fake_user(user_email)

    async def fake_db():
        yield AsyncMock()

    app.dependency_overrides[get_db] = fake_db
    return app


def _fake_chart(chart_id="chart1", organization_id="acme"):
    chart = MagicMock()
    chart.id = chart_id
    chart.organization_id = organization_id
    return chart


_VALID_BODY = {
    "data": [{"date": "2024-01-01", "sales": 10}, {"date": "2024-02-01", "sales": 12}],
    "date_var": "date",
    "target_var": "sales",
    "horizon": 12,
    "models": ["prophet"],
}

_TH2FORECAST_BODY = {"status": "success", "series": [{"group": None, "model": "prophet", "forecast": []}]}


class _Patched:
    """Contexte commun : ChartService.get, ForecastSnapshotStore et
    Th2forecastClient tous patchés dans apowerb.routers.forecast."""

    def __init__(self, *, chart=None, chart_error=None, snapshots=None):
        self.chart = chart
        self.chart_error = chart_error
        self.snapshots = snapshots or []
        self.mock_client_instance = MagicMock()
        self.mock_client_instance.forecast.return_value = dict(_TH2FORECAST_BODY)
        self.mock_upsert = AsyncMock()

    def __enter__(self):
        self._patches = [
            patch("apowerb.routers.forecast.DatabaseChartStore"),
            patch("apowerb.routers.forecast.ChartService"),
            patch("apowerb.routers.forecast.ForecastSnapshotStore"),
            patch("apowerb.routers.forecast.Th2forecastClient"),
        ]
        (self.mock_chart_store_cls, self.mock_chart_service_cls,
         self.mock_snapshot_store_cls, self.mock_client_cls) = [p.start() for p in self._patches]

        mock_service = AsyncMock()
        if self.chart_error is not None:
            mock_service.get.side_effect = self.chart_error
        else:
            mock_service.get.return_value = self.chart
        self.mock_chart_service_cls.return_value = mock_service
        self.mock_service = mock_service

        mock_snapshot_store = MagicMock()
        mock_snapshot_store.list_for_chart = AsyncMock(return_value=self.snapshots)
        mock_snapshot_store.upsert = self.mock_upsert
        self.mock_snapshot_store_cls.return_value = mock_snapshot_store
        self.mock_snapshot_store = mock_snapshot_store

        self.mock_client_cls.return_value = self.mock_client_instance
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()


def test_without_chart_id_nothing_is_stored_and_no_tracking_field():
    app = _build_app()
    client = TestClient(app, raise_server_exceptions=False)

    with _Patched() as ctx:
        resp = client.post("/api/v1/forecast", json=_VALID_BODY)

    assert resp.status_code == 200
    assert "tracking" not in resp.json()
    ctx.mock_chart_service_cls.assert_not_called()
    ctx.mock_snapshot_store_cls.assert_not_called()
    ctx.mock_client_instance.forecast.assert_called_once()


def test_chart_id_belonging_to_another_user_is_a_404_and_engine_is_never_called():
    from apowerb.bi.charts.service import ChartNotFoundError

    app = _build_app()
    client = TestClient(app, raise_server_exceptions=False)

    with _Patched(chart_error=ChartNotFoundError("chart1")) as ctx:
        resp = client.post("/api/v1/forecast", json={**_VALID_BODY, "chart_id": "chart1"})

    assert resp.status_code == 404
    ctx.mock_client_cls.assert_not_called()
    ctx.mock_snapshot_store_cls.assert_not_called()


def test_chart_id_accessible_adds_tracking_and_stores_a_snapshot():
    chart = _fake_chart()
    app = _build_app()
    client = TestClient(app, raise_server_exceptions=False)

    with _Patched(chart=chart, snapshots=[]) as ctx:
        resp = client.post("/api/v1/forecast", json={**_VALID_BODY, "chart_id": "chart1"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["tracking"] == {
        "points": 0, "since": None, "coverage": {}, "mase": None, "breaches": [], "latest_breach": False,
    }
    ctx.mock_upsert.assert_awaited_once()
    kwargs = ctx.mock_upsert.await_args.kwargs
    assert kwargs["chart_id"] == "chart1"
    assert kwargs["owner"] == "alice@example.com"
    assert kwargs["organization_id"] == "acme"
    assert kwargs["history_end"].isoformat() == "2024-02-01"
    assert kwargs["payload"] == _TH2FORECAST_BODY


def test_chart_id_second_call_with_extended_history_gets_points_from_prior_snapshot():
    chart = _fake_chart()
    app = _build_app()
    client = TestClient(app, raise_server_exceptions=False)

    prior_snapshot = MagicMock()
    prior_snapshot.config_hash = None  # calculé dynamiquement plus bas
    prior_snapshot.history_end = None
    prior_snapshot.payload = {
        "series": [{"group": None, "model": "prophet", "forecast": [
            {"date": "2024-03-01", "value": 12.0, "lower_80": 10.0, "upper_80": 14.0},
        ]}]
    }

    from datetime import date

    from apowerb.bi.forecast_tracking import compute_config_hash
    # Le hash réel calculé par la route dépend du payload relayé (sans
    # chart_id) ; on le calcule à l'identique pour que le snapshot "matche".
    from apowerb.routers.forecast import _relay_payload
    from apowerb.schema.forecast_schema import ForecastRequestSchema

    schema_body = ForecastRequestSchema(**_VALID_BODY)
    real_hash = compute_config_hash(_relay_payload(schema_body))
    prior_snapshot.config_hash = real_hash
    prior_snapshot.history_end = date(2024, 2, 1)

    extended_body = {
        **_VALID_BODY,
        "data": [
            {"date": "2024-01-01", "sales": 10},
            {"date": "2024-02-01", "sales": 12},
            {"date": "2024-03-01", "sales": 13},  # réel arrivé après le snapshot précédent
        ],
        "chart_id": "chart1",
    }

    with _Patched(chart=chart, snapshots=[prior_snapshot]):
        resp = client.post("/api/v1/forecast", json=extended_body)

    assert resp.status_code == 200
    tracking = resp.json()["tracking"]
    assert tracking["points"] == 1
    assert tracking["breaches"] == []  # 13 est dans [10, 14]


def test_chart_id_is_never_relayed_to_the_engine():
    chart = _fake_chart()
    app = _build_app()
    client = TestClient(app, raise_server_exceptions=False)

    with _Patched(chart=chart) as ctx:
        client.post("/api/v1/forecast", json={**_VALID_BODY, "chart_id": "chart1"})

    sent_payload = ctx.mock_client_instance.forecast.call_args.args[0]
    assert "chart_id" not in sent_payload


def test_hierarchy_and_reconciliation_are_relayed_when_present():
    app = _build_app()
    client = TestClient(app, raise_server_exceptions=False)

    body = {
        **_VALID_BODY,
        "group_var": "store",
        "hierarchy": ["region"],
        "reconciliation": "mint",
    }
    with _Patched() as ctx:
        resp = client.post("/api/v1/forecast", json=body)

    assert resp.status_code == 200
    sent_payload = ctx.mock_client_instance.forecast.call_args.args[0]
    assert sent_payload["hierarchy"] == ["region"]
    assert sent_payload["reconciliation"] == "mint"


def test_hierarchy_and_reconciliation_absent_when_not_provided():
    app = _build_app()
    client = TestClient(app, raise_server_exceptions=False)

    with _Patched() as ctx:
        client.post("/api/v1/forecast", json=_VALID_BODY)

    sent_payload = ctx.mock_client_instance.forecast.call_args.args[0]
    assert "hierarchy" not in sent_payload
    assert "reconciliation" not in sent_payload


def test_storage_failure_logs_and_still_returns_the_forecast_without_tracking():
    chart = _fake_chart()
    app = _build_app()
    client = TestClient(app, raise_server_exceptions=False)

    with _Patched(chart=chart) as ctx:
        ctx.mock_upsert.side_effect = RuntimeError("db down")
        resp = client.post("/api/v1/forecast", json={**_VALID_BODY, "chart_id": "chart1"})

    assert resp.status_code == 200
    body = resp.json()
    assert "tracking" not in body
    assert body["status"] == "success"


def test_access_refusal_happens_before_the_engine_is_constructed():
    from apowerb.bi.charts.service import ChartNotFoundError

    app = _build_app()
    client = TestClient(app, raise_server_exceptions=False)

    with _Patched(chart_error=ChartNotFoundError("chart1")) as ctx:
        client.post("/api/v1/forecast", json={**_VALID_BODY, "chart_id": "chart1"})

    ctx.mock_client_cls.assert_not_called()

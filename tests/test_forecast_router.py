"""Integration tests for routers/forecast.py (POST /api/v1/forecast).

Verifies:
- The route requires authentication (401 without one).
- A missing TH2FORECAST_URL answers 503 with the contract's message.
- th2forecast's own errors (400) are relayed with their status and body.
- A successful call relays th2forecast's response body untouched.
- Pydantic validation rejects a bad horizon/models before the client is
  ever built.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient


def _fake_user(email: str = "alice@example.com", user_id: int = 1):
    u = MagicMock()
    u.email = email
    u.user_id = user_id
    u.role = "USER"
    return u


def _build_app(*, authenticated: bool = True):
    from apowerb.auth.dependencies import get_current_user
    from apowerb.routers.forecast import router

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    async def _user_override():
        if not authenticated:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
        return _fake_user()

    app.dependency_overrides[get_current_user] = _user_override
    return app


_VALID_BODY = {
    "data": [{"date": "2024-01-01", "sales": 10}, {"date": "2024-02-01", "sales": 12}],
    "date_var": "date",
    "target_var": "sales",
    "horizon": 12,
    "models": ["prophet"],
}


def test_forecast_requires_auth():
    app = _build_app(authenticated=False)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/api/v1/forecast", json=_VALID_BODY)
    assert resp.status_code == 401


def test_forecast_returns_503_when_not_configured():
    app = _build_app()
    client = TestClient(app, raise_server_exceptions=False)

    with patch("apowerb.routers.forecast.Th2forecastClient") as mock_ctor:
        from apowerb.integrations.th2forecast_client import Th2forecastNotConfigured

        mock_ctor.side_effect = Th2forecastNotConfigured("TH2FORECAST_URL is not configured")
        resp = client.post("/api/v1/forecast", json=_VALID_BODY)

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "error"
    assert "TH2FORECAST_URL" in body["errors"][0]["message"]


def test_forecast_relays_th2forecast_success_body_untouched():
    app = _build_app()
    client = TestClient(app, raise_server_exceptions=False)

    th2forecast_body = {"status": "success", "api_version": "1", "series": [{"model": "prophet"}]}
    with patch("apowerb.routers.forecast.Th2forecastClient") as mock_ctor:
        mock_instance = MagicMock()
        mock_instance.forecast.return_value = th2forecast_body
        mock_ctor.return_value = mock_instance
        resp = client.post("/api/v1/forecast", json=_VALID_BODY)

    assert resp.status_code == 200
    assert resp.json() == th2forecast_body
    mock_instance.forecast.assert_called_once()


def test_forecast_relays_th2forecast_400_error_with_its_status_and_body():
    app = _build_app()
    client = TestClient(app, raise_server_exceptions=False)

    error_body = {"status": "error", "errors": [{"field": "date_var", "message": "colonne absente"}]}
    with patch("apowerb.routers.forecast.Th2forecastClient") as mock_ctor:
        from apowerb.integrations.th2forecast_client import Th2forecastAPIError

        mock_instance = MagicMock()
        mock_instance.forecast.side_effect = Th2forecastAPIError(400, error_body)
        mock_ctor.return_value = mock_instance
        resp = client.post("/api/v1/forecast", json=_VALID_BODY)

    assert resp.status_code == 400
    assert resp.json() == error_body


def test_forecast_rejects_horizon_out_of_range_before_calling_client():
    app = _build_app()
    client = TestClient(app, raise_server_exceptions=False)

    bad_body = dict(_VALID_BODY, horizon=400)
    with patch("apowerb.routers.forecast.Th2forecastClient") as mock_ctor:
        resp = client.post("/api/v1/forecast", json=bad_body)

    assert resp.status_code == 422
    mock_ctor.assert_not_called()


def test_forecast_rejects_unknown_model_before_calling_client():
    app = _build_app()
    client = TestClient(app, raise_server_exceptions=False)

    bad_body = dict(_VALID_BODY, models=["xgboost"])
    with patch("apowerb.routers.forecast.Th2forecastClient") as mock_ctor:
        resp = client.post("/api/v1/forecast", json=bad_body)

    assert resp.status_code == 422
    mock_ctor.assert_not_called()

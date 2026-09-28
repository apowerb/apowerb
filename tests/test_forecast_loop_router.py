"""Tests de POST /api/v1/forecast avec `chart_id` — boucle fermée (contrat
étape 5 §3) : accès, relais, suivi, instantané, non-blocage de la boucle
asyncio. th2forecast et la base sont tous les deux remplacés ; la logique de
calcul du suivi est testée à part dans tests/test_forecast_tracking.py."""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
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

# Réponse moteur : `history` (dates régularisées ISO par th2forecast) porte
# les actuels du suivi, distincts du `data` brut envoyé dans la requête.
_TH2FORECAST_BODY = {
    "status": "success",
    "series": [
        {
            "group": None,
            "level": None,
            "model": "prophet",
            "history": [{"date": "2024-01-01", "value": 10}, {"date": "2024-02-01", "value": 12}],
            "forecast": [],
        }
    ],
}

# Ce que la route doit conserver dans l'instantané (contrat étape 5 §3) :
# group/level/model/forecast seulement, jamais `history` ni le reste.
_PRUNED_PAYLOAD = {"series": [{"group": None, "level": None, "model": "prophet", "forecast": []}]}


class _Patched:
    """Contexte commun : ChartService.get, ForecastSnapshotStore et
    Th2forecastClient tous patchés dans apowerb.routers.forecast."""

    def __init__(self, *, chart=None, chart_error=None, snapshots=None, th2forecast_body=None):
        self.chart = chart
        self.chart_error = chart_error
        self.snapshots = snapshots or []
        self.mock_client_instance = MagicMock()
        self.mock_client_instance.forecast.return_value = dict(th2forecast_body or _TH2FORECAST_BODY)
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


def test_chart_id_accessible_adds_tracking_and_stores_a_pruned_snapshot():
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
    # history_end vient de series[].history (moteur), pas de data brut.
    assert kwargs["history_end"].isoformat() == "2024-02-01"
    # Contrat étape 5 §3 : group/level/model/forecast seulement — pas `history`.
    assert kwargs["payload"] == _PRUNED_PAYLOAD


def test_chart_id_second_call_with_extended_history_gets_points_from_prior_snapshot():
    chart = _fake_chart()
    app = _build_app()
    client = TestClient(app, raise_server_exceptions=False)

    from datetime import date

    from apowerb.bi.forecast_tracking import compute_config_hash
    # Le hash réel calculé par la route dépend du payload relayé (sans
    # chart_id) ; on le calcule à l'identique pour que le snapshot "matche".
    from apowerb.routers.forecast import _relay_payload
    from apowerb.schema.forecast_schema import ForecastRequestSchema

    schema_body = ForecastRequestSchema(**_VALID_BODY)
    real_hash = compute_config_hash(_relay_payload(schema_body))

    prior_snapshot = MagicMock()
    prior_snapshot.config_hash = real_hash
    prior_snapshot.history_end = date(2024, 2, 1)
    prior_snapshot.payload = {
        "series": [{"group": None, "level": None, "model": "prophet", "forecast": [
            {"date": "2024-03-01", "value": 12.0, "lower_80": 10.0, "upper_80": 14.0},
        ]}]
    }

    extended_response = {
        "status": "success",
        "series": [{
            "group": None, "level": None, "model": "prophet",
            "history": [
                {"date": "2024-01-01", "value": 10},
                {"date": "2024-02-01", "value": 12},
                {"date": "2024-03-01", "value": 13},  # réel arrivé après le snapshot précédent
            ],
            "forecast": [],
        }],
    }

    with _Patched(chart=chart, snapshots=[prior_snapshot], th2forecast_body=extended_response):
        resp = client.post("/api/v1/forecast", json={**_VALID_BODY, "chart_id": "chart1"})

    assert resp.status_code == 200
    tracking = resp.json()["tracking"]
    assert tracking["points"] == 1
    assert tracking["breaches"] == []  # 13 est dans [10, 14]


def test_actuals_come_from_engine_history_not_from_raw_request_data():
    """`data` porte un format de date différent de celui du moteur (avec
    heure) et un groupe numérique : si la route s'en servait pour le suivi,
    le rapprochement échouerait silencieusement (points=0) ou `_history_end`
    planterait sur `fromisoformat`. La réponse du moteur, elle, est déjà
    régularisée (ISO, group en chaîne) et doit être la seule source."""
    chart = _fake_chart()
    app = _build_app()
    client = TestClient(app, raise_server_exceptions=False)

    odd_body = {
        **_VALID_BODY,
        "group_var": "store",
        "data": [
            {"date": "2024-01-01 00:00:00", "sales": 10, "store": 42},
            {"date": "2024/02/01", "sales": 12, "store": 42},
        ],
        "chart_id": "chart1",
    }
    engine_response = {
        "status": "success",
        "series": [{
            "group": "42", "level": None, "model": "prophet",
            "history": [{"date": "2024-01-01", "value": 10}, {"date": "2024-02-01", "value": 12}],
            "forecast": [],
        }],
    }

    with _Patched(chart=chart, th2forecast_body=engine_response) as ctx:
        resp = client.post("/api/v1/forecast", json=odd_body)

    assert resp.status_code == 200  # ni crash sur fromisoformat, ni 500
    assert resp.json()["tracking"]["points"] == 0  # premier calcul, rien à comparer encore
    kwargs = ctx.mock_upsert.await_args.kwargs
    assert kwargs["history_end"].isoformat() == "2024-02-01"


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


def test_empty_hierarchy_list_is_accepted_with_group_var():
    """Contrat étape 5 §2 : une hiérarchie vide (Total seul) est valide."""
    app = _build_app()
    client = TestClient(app, raise_server_exceptions=False)

    body = {**_VALID_BODY, "group_var": "store", "hierarchy": []}
    with _Patched() as ctx:
        resp = client.post("/api/v1/forecast", json=body)

    assert resp.status_code == 200
    sent_payload = ctx.mock_client_instance.forecast.call_args.args[0]
    assert sent_payload["hierarchy"] == []


def test_hierarchy_without_group_var_is_rejected():
    app = _build_app()
    client = TestClient(app, raise_server_exceptions=False)

    body = {**_VALID_BODY, "hierarchy": ["region"]}
    with _Patched() as ctx:
        resp = client.post("/api/v1/forecast", json=body)

    assert resp.status_code == 422
    ctx.mock_client_cls.assert_not_called()


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


@pytest.mark.asyncio
async def test_a_blocking_th2forecast_client_does_not_freeze_the_event_loop(monkeypatch):
    """Régression : la route est `async def`, mais `client.forecast()` est un
    appel bloquant (requests + sleep de polling). Sans `asyncio.to_thread`,
    tout le cœur gèlerait pendant le calcul. Preuve : une coroutine "ticker"
    concurrente doit continuer à avancer pendant l'appel bloquant."""
    from apowerb.routers import forecast as forecast_router

    class SlowClient:
        def forecast(self, payload):
            time.sleep(0.3)  # bloquant, volontairement pas asyncio.sleep
            return dict(_TH2FORECAST_BODY)

    monkeypatch.setattr(forecast_router, "Th2forecastClient", lambda: SlowClient())

    ticks = {"n": 0}

    async def ticker():
        while True:
            ticks["n"] += 1
            await asyncio.sleep(0.01)

    from apowerb.schema.forecast_schema import ForecastRequestSchema

    body = ForecastRequestSchema(**_VALID_BODY)
    user = _fake_user()
    db = AsyncMock()

    ticker_task = asyncio.create_task(ticker())
    await forecast_router.create_forecast(body, user, db)
    ticker_task.cancel()

    # ~0.3s d'appel bloquant / 0.01s de période : si la boucle était gelée,
    # le ticker n'aurait presque pas progressé (0 ou 1 tick).
    assert ticks["n"] > 5


# ---------------------------------------------------------------------------
# Contrat etape 7 SS2 : feedback relaye, explanation, notification, adjustments.
# ---------------------------------------------------------------------------


class _PatchedS7(_Patched):
    """Meme contexte que _Patched, plus notify_breach patche (jamais de
    vraie DB/notification ici -- teste a part dans
    tests/test_forecast_alert_store.py)."""

    def __enter__(self):
        super().__enter__()
        self._notify_patch = patch("apowerb.routers.forecast.notify_breach", new_callable=AsyncMock)
        self.mock_notify = self._notify_patch.start()
        self.mock_notify.return_value = True
        self._link_patch = patch(
            "apowerb.routers.forecast.dashboard_link_for_chart", new_callable=AsyncMock, return_value="/bi/dash1"
        )
        self.mock_link = self._link_patch.start()
        return self

    def __exit__(self, *exc):
        self._link_patch.stop()
        self._notify_patch.stop()
        super().__exit__(*exc)


def test_feedback_is_built_from_snapshots_and_relayed_to_the_engine():
    from datetime import date

    from apowerb.bi.forecast_tracking import compute_config_hash
    from apowerb.routers.forecast import _relay_payload
    from apowerb.schema.forecast_schema import ForecastRequestSchema

    chart = _fake_chart()
    app = _build_app()
    client = TestClient(app, raise_server_exceptions=False)

    real_hash = compute_config_hash(_relay_payload(ForecastRequestSchema(**_VALID_BODY)))
    prior_snapshot = MagicMock()
    prior_snapshot.config_hash = real_hash
    prior_snapshot.history_end = date(2024, 1, 1)
    prior_snapshot.payload = {
        "series": [{"group": None, "level": None, "model": "prophet", "forecast": [
            {"date": "2024-02-01", "value": 12.0, "lower_80": 10.0, "upper_80": 14.0},
        ]}]
    }

    with _PatchedS7(chart=chart, snapshots=[prior_snapshot]) as ctx:
        client.post("/api/v1/forecast", json={**_VALID_BODY, "chart_id": "chart1"})

    sent_payload = ctx.mock_client_instance.forecast.call_args.args[0]
    assert sent_payload["feedback"] == [
        {"group": None, "level": None, "points": [
            {"date": "2024-02-01", "value": 12.0, "lower_80": 10.0, "upper_80": 14.0},
        ]},
    ]


def test_client_sent_feedback_is_discarded_server_builds_its_own():
    chart = _fake_chart()
    app = _build_app()
    client = TestClient(app, raise_server_exceptions=False)

    with _PatchedS7(chart=chart, snapshots=[]) as ctx:
        client.post(
            "/api/v1/forecast",
            json={**_VALID_BODY, "chart_id": "chart1", "feedback": [{"group": "evil", "level": None, "points": []}]},
        )

    sent_payload = ctx.mock_client_instance.forecast.call_args.args[0]
    # Pas de snapshot pertinent -> feedback vide -> cle absente, jamais celle du client.
    assert "feedback" not in sent_payload


def test_no_prior_snapshot_means_no_feedback_key():
    app = _build_app()
    client = TestClient(app, raise_server_exceptions=False)
    chart = _fake_chart()

    with _PatchedS7(chart=chart, snapshots=[]) as ctx:
        client.post("/api/v1/forecast", json={**_VALID_BODY, "chart_id": "chart1"})

    sent_payload = ctx.mock_client_instance.forecast.call_args.args[0]
    assert "feedback" not in sent_payload


def test_planted_breach_gets_an_explanation_and_triggers_one_notification():
    chart = _fake_chart()
    app = _build_app()
    client = TestClient(app, raise_server_exceptions=False)

    from datetime import date

    from apowerb.bi.forecast_tracking import compute_config_hash
    from apowerb.routers.forecast import _relay_payload
    from apowerb.schema.forecast_schema import ForecastRequestSchema

    real_hash = compute_config_hash(_relay_payload(ForecastRequestSchema(**_VALID_BODY)))
    prior_snapshot = MagicMock()
    prior_snapshot.config_hash = real_hash
    prior_snapshot.history_end = date(2024, 2, 1)
    prior_snapshot.payload = {
        "series": [{"group": None, "level": None, "model": "prophet", "forecast": [
            {"date": "2024-03-01", "value": 12.0, "lower_80": 10.0, "upper_80": 14.0},
        ]}]
    }
    breach_response = {
        "status": "success",
        "series": [{
            "group": None, "level": None, "model": "prophet",
            "history": [
                {"date": "2024-01-01", "value": 10}, {"date": "2024-02-01", "value": 12},
                {"date": "2024-03-01", "value": 99},
            ],
            "forecast": [],
        }],
    }

    with _PatchedS7(chart=chart, snapshots=[prior_snapshot], th2forecast_body=breach_response) as ctx:
        resp = client.post("/api/v1/forecast", json={**_VALID_BODY, "chart_id": "chart1"})

    tracking = resp.json()["tracking"]
    assert tracking["breaches"][0]["explanation"]["kind"] == "spike"
    ctx.mock_notify.assert_awaited_once()
    kwargs = ctx.mock_notify.await_args.kwargs
    assert kwargs["chart_id"] == "chart1"
    assert kwargs["group"] is None
    assert kwargs["date"] == date(2024, 3, 1)
    # Lien vers le tableau de bord (route UI /bi/<dashboardId>), pas vers le graphique.
    assert kwargs["link"] == "/bi/dash1"


def test_notification_failure_never_drops_the_tracking_field():
    chart = _fake_chart()
    app = _build_app()
    client = TestClient(app, raise_server_exceptions=False)

    with _PatchedS7(chart=chart, snapshots=[]) as ctx:
        ctx.mock_notify.side_effect = RuntimeError("bus down")
        resp = client.post("/api/v1/forecast", json={**_VALID_BODY, "chart_id": "chart1"})

    assert resp.status_code == 200
    assert "tracking" in resp.json()

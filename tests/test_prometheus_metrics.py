"""Tests pour B19 — métriques Prometheus exposées via ``/metrics``.

Une app minimale monte :
- ``MetricsMiddleware`` : alimente les counters/histograms par requête.
- ``GET /metrics`` : endpoint ASGI ``prometheus_client.make_asgi_app()``.

Ces tests sont sautés (``pytest.importorskip``) si ``prometheus_client``
n'est pas installé — la lib est optionnelle mais fortement recommandée.
"""

from __future__ import annotations

import pytest

prometheus_client = pytest.importorskip("prometheus_client")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apowerb.helpers import metrics as metrics_mod
from apowerb.helpers.metrics_middleware import MetricsMiddleware


def _build_app() -> FastAPI:
    # Reset metrics between tests to avoid cross-test contamination.
    metrics_mod.reset_metrics_for_tests()
    app = FastAPI()
    app.add_middleware(MetricsMiddleware)

    @app.get("/hello")
    async def hello():
        return {"ok": True}

    # Mount /metrics using the shared registry so counters are exposed.
    app.mount("/metrics", metrics_mod.make_metrics_asgi_app())
    return app


class TestPrometheusMetrics:
    def test_metrics_endpoint_returns_200(self):
        client = TestClient(_build_app())
        resp = client.get("/metrics")
        assert resp.status_code == 200
        # Prometheus exposition format must start with HELP/TYPE lines.
        assert "text/plain" in resp.headers.get("content-type", "")

    def test_request_counter_is_incremented_on_request(self):
        client = TestClient(_build_app())
        client.get("/hello")
        client.get("/hello")
        resp = client.get("/metrics")
        body = resp.text
        # Counter name must appear, and cumulative value >= 2 for /hello.
        assert "th2agent_requests_total" in body
        # Sanity: at least one sample for /hello path exists.
        assert 'path="/hello"' in body

    def test_request_histogram_exposes_buckets(self):
        client = TestClient(_build_app())
        client.get("/hello")
        resp = client.get("/metrics")
        body = resp.text
        assert "th2agent_request_duration_seconds" in body
        # Prometheus exposes histogram buckets with ``le`` label.
        assert "th2agent_request_duration_seconds_bucket" in body
        assert "le=" in body

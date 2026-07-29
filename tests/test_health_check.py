"""Tests pour B19 — healthchecks ``/health/live`` et ``/health/ready``.

``/health/live`` : sanity check, retourne 200 tant que le process est
vivant (pas de dépendance externe testée).

``/health/ready`` : vérifie les dépendances critiques :

- DB : ``SELECT 1`` via la session SQLAlchemy async.
- Fernet (``ENCRYPT_KEY``) configurée (``encryptor.fernet`` non ``None``).
- RAG API / Stripe : meilleurs efforts, timeout 2s, n'invalident pas la
  readiness s'ils retournent 5xx (dépendance soft) — ils sont seulement
  rapportés dans le body.

Retour 200 quand toutes les dépendances hard (DB + Fernet) sont OK,
503 sinon, avec un body JSON listant l'état de chaque composant.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from th2agent.routers import health as health_module


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(health_module.router)
    return app


class TestLiveness:
    def test_live_returns_200(self):
        client = TestClient(_build_app())
        resp = client.get("/health/live")
        assert resp.status_code == 200
        body = resp.json()
        assert body.get("status") == "alive"


class TestReadiness:
    def test_ready_returns_200_when_all_deps_ok(self, monkeypatch):
        async def ok_db_check():
            return True, None

        def ok_fernet_check():
            return True, None

        monkeypatch.setattr(health_module, "_check_db", ok_db_check)
        monkeypatch.setattr(health_module, "_check_fernet", ok_fernet_check)

        client = TestClient(_build_app())
        resp = client.get("/health/ready")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ready"
        assert body["checks"]["database"]["ok"] is True
        assert body["checks"]["fernet"]["ok"] is True

    def test_ready_returns_503_when_db_ko(self, monkeypatch):
        async def ko_db_check():
            return False, "connection refused"

        def ok_fernet_check():
            return True, None

        monkeypatch.setattr(health_module, "_check_db", ko_db_check)
        monkeypatch.setattr(health_module, "_check_fernet", ok_fernet_check)

        client = TestClient(_build_app())
        resp = client.get("/health/ready")
        assert resp.status_code == 503
        body = resp.json()
        detail = body.get("detail") or body
        assert detail["status"] == "not_ready"
        assert detail["checks"]["database"]["ok"] is False
        assert "connection refused" in detail["checks"]["database"]["error"]

    def test_ready_returns_503_when_fernet_missing(self, monkeypatch):
        async def ok_db_check():
            return True, None

        def ko_fernet_check():
            return False, "ENCRYPT_KEY not configured"

        monkeypatch.setattr(health_module, "_check_db", ok_db_check)
        monkeypatch.setattr(health_module, "_check_fernet", ko_fernet_check)

        client = TestClient(_build_app())
        resp = client.get("/health/ready")
        assert resp.status_code == 503
        body = resp.json()
        detail = body.get("detail") or body
        assert detail["checks"]["fernet"]["ok"] is False

    def test_ready_db_timeout_returns_503(self, monkeypatch):
        import asyncio

        async def timeout_db_check():
            # Simulate a DB call that exceeds the 2s budget. The router
            # must not hang — it should apply its own timeout and report
            # failure.
            await asyncio.sleep(5)
            return True, None

        def ok_fernet_check():
            return True, None

        monkeypatch.setattr(health_module, "_check_db", timeout_db_check)
        monkeypatch.setattr(health_module, "_check_fernet", ok_fernet_check)
        # Shorten timeout to keep the test fast.
        monkeypatch.setattr(health_module, "DEPENDENCY_TIMEOUT_S", 0.2)

        client = TestClient(_build_app())
        resp = client.get("/health/ready")
        assert resp.status_code == 503
        body = resp.json()
        detail = body.get("detail") or body
        assert detail["checks"]["database"]["ok"] is False
        assert "timeout" in detail["checks"]["database"]["error"].lower()

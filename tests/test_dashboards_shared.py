"""Régression pour bi/dashboards/router.py::list_shared_dashboards.

Bug prouvé en prod (journalctl dev, 96 occ./7j) : un dashboard publié dont
``created_by`` est vide ou malformé faisait lever ``ValueError: Invalid email
format`` par ``get_domain_from_email`` → 500 sur ``GET /dashboards/shared``.

Le domaine inconnu doit être traité comme « hors organisation » (dashboard non
partagé en visibilité ORGANIZATION) et ne jamais casser la requête.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient

from apowerb.bi.dashboards.core import DashboardStatus, DashboardVisibility

VIEWER_EMAIL = "alice@example.com"


def _dash(*, id_, created_by, visibility, status_=DashboardStatus.PUBLISHED):
    d = MagicMock()
    d.id = id_
    d.title = f"dash-{id_}"
    d.description = None
    d.slug = f"slug-{id_}"
    d.status = status_
    d.visibility = visibility
    d.version = 1
    d.components = []
    d.created_by = created_by
    d.agent_id = None
    d.created_at = datetime.now(timezone.utc)
    d.updated_at = datetime.now(timezone.utc)
    return d


def _build_app(dashboards, *, email=VIEWER_EMAIL):
    from apowerb.auth.dependencies import get_current_user
    from apowerb.helpers.database import get_db
    from apowerb.bi.dashboards.router import router

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    async def _user_override():
        u = MagicMock()
        u.email = email
        u.user_id = 1
        u.role = "USER"
        return u

    async def _db_override():
        yield MagicMock()

    app.dependency_overrides[get_current_user] = _user_override
    app.dependency_overrides[get_db] = _db_override

    class _FakeStore:
        def __init__(self, *a, **k):
            pass

        async def list(self, *, page, page_size):
            return list(dashboards), len(dashboards)

    return app, _FakeStore


def _patched_client(dashboards, *, email=VIEWER_EMAIL):
    app, store_cls = _build_app(dashboards, email=email)
    p = patch("apowerb.bi.db_stores.DatabaseDashboardStore", store_cls)
    p.start()
    client = TestClient(app, raise_server_exceptions=False)
    return client, p


class TestSharedDashboardsMalformedCreatedBy:
    def test_org_dashboard_with_empty_created_by_does_not_500(self):
        dashboards = [
            _dash(id_="d1", created_by="", visibility=DashboardVisibility.ORGANIZATION),
            _dash(id_="d2", created_by=None, visibility=DashboardVisibility.ORGANIZATION),
            _dash(id_="d3", created_by="pasunemail", visibility=DashboardVisibility.ORGANIZATION),
        ]
        client, p = _patched_client(dashboards)
        try:
            resp = client.get("/api/v1/dashboards/shared")
        finally:
            p.stop()
        assert resp.status_code == 200, resp.text
        # Aucun de ces dashboards n'est partageable (domaine inconnu) → liste vide.
        assert resp.json()["items"] == []

    def test_org_dashboard_same_domain_is_shared(self):
        dashboards = [
            _dash(id_="ok", created_by="bob@example.com", visibility=DashboardVisibility.ORGANIZATION),
            _dash(id_="other", created_by="carol@autre.com", visibility=DashboardVisibility.ORGANIZATION),
            _dash(id_="bad", created_by="", visibility=DashboardVisibility.ORGANIZATION),
        ]
        client, p = _patched_client(dashboards)
        try:
            resp = client.get("/api/v1/dashboards/shared")
        finally:
            p.stop()
        assert resp.status_code == 200, resp.text
        ids = {it["id"] for it in resp.json()["items"]}
        assert ids == {"ok"}

    def test_public_dashboard_always_shared_even_with_bad_created_by(self):
        dashboards = [
            _dash(id_="pub", created_by="", visibility=DashboardVisibility.PUBLIC),
        ]
        client, p = _patched_client(dashboards)
        try:
            resp = client.get("/api/v1/dashboards/shared")
        finally:
            p.stop()
        assert resp.status_code == 200, resp.text
        ids = {it["id"] for it in resp.json()["items"]}
        assert ids == {"pub"}

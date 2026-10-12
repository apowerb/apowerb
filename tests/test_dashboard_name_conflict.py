"""A dashboard name already taken in the organization is a 409, not a 500.

``business_intelligence`` has a unique constraint on (name, type,
organization_id, project_id); the store re-raises the IntegrityError.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from apowerb.bi.dashboards.service import DashboardService

USER = "alice@example.com"


class _TakenNameStore:
    """Every save hits the unique constraint, like a name already in the organization."""

    def __init__(self, existing=None):
        self._existing = existing

    async def get(self, dashboard_id):
        return self._existing

    async def get_by_slug(self, slug):
        return None

    async def save(self, dashboard):
        raise IntegrityError("INSERT", {}, Exception("uq_bi_name_type_org_project"))


def _client(store):
    from apowerb.auth.dependencies import get_current_user
    from apowerb.bi.dashboards.router import router
    from apowerb.bi.dependencies import get_dashboard_service

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    async def _user():
        u = MagicMock()
        u.email = USER
        return u

    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[get_dashboard_service] = lambda: DashboardService(store)
    return TestClient(app, raise_server_exceptions=False)


def test_create_with_a_taken_name_is_a_409():
    resp = _client(_TakenNameStore()).post("/api/v1/dashboards", json={"title": "test"})
    assert resp.status_code == 409, resp.text
    assert "test" in resp.json()["detail"]


def test_rename_to_a_taken_name_is_a_409():
    from apowerb.bi.dashboards.core import Dashboard

    existing = Dashboard.create(title="Ventes", created_by=USER)
    resp = _client(_TakenNameStore(existing)).patch(
        f"/api/v1/dashboards/{existing.id}", json={"title": "test"}
    )
    assert resp.status_code == 409, resp.text
    assert "test" in resp.json()["detail"]

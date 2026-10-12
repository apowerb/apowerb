"""Spec v2 routes: /dashboards/{id}/data, /public/dashboards/{slug}/data,
/dashboards/{id}/spec.

The access rules are those of the existing dashboard routes: owner-scoped
service for the authenticated ones, published + visibility for the public one.
Data is always read as the dashboard owner, never as the viewer.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apowerb.bi.dashboards.core import Dashboard, DashboardStatus, DashboardVisibility
from apowerb.bi.dashboards.router import router as dashboards_router
from apowerb.bi.dashboards.service import DashboardService, InMemoryDashboardStore
from apowerb.bi.spec import engine, sources
from apowerb.bi.spec.router import router as spec_router
from tests.helpers.bi_spec_data import OWNER, build_dashboard, kpi, pipeline_source, weekly_sales

OUTSIDER = "someone@other.org"
COLLEAGUE = "colleague@example.com"


class ScopedStore(InMemoryDashboardStore):
    """In-memory store with the owner scoping of DatabaseDashboardStore."""

    def __init__(self, shared: dict, owner: str | None) -> None:
        self._data = shared
        self._owner = owner

    async def get(self, dashboard_id):
        d = self._data.get(dashboard_id)
        if d is None or (self._owner and (d.created_by or "").lower() != self._owner.lower()):
            return None
        return d

    async def save(self, dashboard):
        self._data[dashboard.id] = dashboard
        return dashboard

    async def get_by_slug(self, slug):
        return next((d for d in self._data.values() if d.slug == slug), None)


def _user(email):
    user = MagicMock()
    user.email = email
    return user


@pytest.fixture(autouse=True)
def _fresh_caches():
    engine.clear_caches()
    yield
    engine.clear_caches()


class Env:
    def __init__(self) -> None:
        self.shared: dict[str, Dashboard] = {}
        self.owners_seen: list[str] = []
        self.frame = weekly_sales(weeks=10)

    def add(self, dashboard: Dashboard) -> Dashboard:
        self.shared[dashboard.id] = dashboard
        return dashboard

    def app(self, viewer: str | None) -> FastAPI:
        from apowerb.auth.dependencies import get_current_user, get_optional_user
        from apowerb.bi.dependencies import get_dashboard_service
        from apowerb.helpers.database import get_db

        app = FastAPI()
        app.include_router(dashboards_router, prefix="/api/v1")
        app.include_router(spec_router, prefix="/api/v1")

        async def fake_db():
            yield AsyncMock()

        app.dependency_overrides[get_db] = fake_db
        app.dependency_overrides[get_optional_user] = lambda: _user(viewer) if viewer else None
        if viewer:
            app.dependency_overrides[get_current_user] = lambda: _user(viewer)
            app.dependency_overrides[get_dashboard_service] = lambda: DashboardService(ScopedStore(self.shared, viewer))
        return app

    def client(self, viewer: str | None) -> TestClient:
        return TestClient(self.app(viewer))

    def patched(self):
        env = self

        async def fake_load(dataset_id, owner, sheet=None):
            env.owners_seen.append(owner)
            return env.frame

        store_cls = lambda db, owner=None: ScopedStore(env.shared, owner)  # noqa: E731
        return _Patches(
            patch.object(sources, "load_dataset", fake_load),
            patch("apowerb.bi.db_stores.DatabaseDashboardStore", store_cls),
        )


class _Patches:
    def __init__(self, *patches):
        self.patches = patches

    def __enter__(self):
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self.patches):
            p.stop()


@pytest.fixture
def env():
    return Env()


def make(env: Env, **kw) -> Dashboard:
    d = build_dashboard({"k": kpi("n"), "v": kpi("ventes_tot")}, **kw)
    return env.add(d)


class TestDashboardData:
    def test_owner_gets_computed_components(self, env):
        d = make(env)
        with env.patched():
            resp = env.client(OWNER).post(f"/api/v1/dashboards/{d.id}/data", json={"filters": {"famille": "Alpha"}})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["dashboard_id"] == d.id
        assert body["components"]["k"]["value"]["value"] == 10  # 10 weeks, one family
        assert body["filters"]["famille"]["label"] == "Alpha"
        assert body["filter_options"]["famille"]
        assert env.owners_seen == [OWNER]

    def test_components_subset(self, env):
        d = make(env)
        with env.patched():
            resp = env.client(OWNER).post(f"/api/v1/dashboards/{d.id}/data", json={"components": ["v"]})
        assert list(resp.json()["components"]) == ["v"]

    def test_other_user_is_refused_and_nothing_is_read(self, env):
        d = make(env)
        with env.patched():
            resp = env.client(OUTSIDER).post(f"/api/v1/dashboards/{d.id}/data", json={})
        assert resp.status_code == 404
        assert env.owners_seen == []

    def test_unknown_dashboard_and_missing_auth(self, env):
        with env.patched():
            assert env.client(OWNER).post("/api/v1/dashboards/nope/data", json={}).status_code == 404

    def test_role_gate_like_get_dashboard(self, env):
        d = env.add(build_dashboard({"k": kpi("n")}).model_copy(update={"permissions": ["admin"]}))
        with env.patched():
            c = env.client(OWNER)
            assert c.post(f"/api/v1/dashboards/{d.id}/data?role=viewer", json={}).status_code == 403
            assert c.post(f"/api/v1/dashboards/{d.id}/data?role=admin", json={}).status_code == 200
            assert c.get(f"/api/v1/dashboards/{d.id}?role=viewer").status_code == 403  # same rule as GET

    def test_invalid_filter_value_is_422(self, env):
        d = make(env)
        with env.patched():
            resp = env.client(OWNER).post(f"/api/v1/dashboards/{d.id}/data", json={"filters": {"periode": "7y"}})
        assert resp.status_code == 422
        assert "7y" in resp.json()["detail"][0]


class TestUnexpectedFailure:
    def test_unexpected_error_is_a_generic_500_without_trace(self, env):
        d = make(env)
        boom = AsyncMock(side_effect=RuntimeError("secret internal detail"))
        with env.patched(), patch("apowerb.bi.spec.router.compute_dashboard", boom):
            resp = env.client(OWNER).post(f"/api/v1/dashboards/{d.id}/data", json={})
        assert resp.status_code == 500
        assert "secret" not in resp.text and "Traceback" not in resp.text

    async def test_cached_response_is_a_deep_copy(self, env):
        from apowerb.bi.spec.models import DashboardDataRequest

        d = make(env)
        with env.patched():
            first = await engine.compute_dashboard(d, DashboardDataRequest(), owner=OWNER)
            first.components["k"].meta.warnings.append("mutation")
            second = await engine.compute_dashboard(d, DashboardDataRequest(), owner=OWNER)
            third = await engine.compute_dashboard(d, DashboardDataRequest(), owner=OWNER)
        assert second.components["k"].meta.warnings == []
        assert second.components["k"] is not third.components["k"]


class TestPublicData:
    def publish(self, env, visibility=DashboardVisibility.PUBLIC, published=True) -> Dashboard:
        d = make(env)
        d = d.model_copy(update={
            "slug": "pilotage",
            "status": DashboardStatus.PUBLISHED if published else DashboardStatus.DRAFT,
            "visibility": visibility,
        })
        return env.add(d)

    def post(self, env, viewer, slug="pilotage", body=None):
        with env.patched():
            return env.client(viewer).post(f"/api/v1/public/dashboards/{slug}/data", json=body or {})

    def test_published_public_dashboard_is_served_with_the_owner_access(self, env):
        self.publish(env)
        resp = self.post(env, OUTSIDER)
        assert resp.status_code == 200, resp.text
        assert resp.json()["components"]["v"]["value"]["value"] > 0
        assert env.owners_seen == [OWNER]  # never the viewer

    def test_unpublished_dashboard_is_404(self, env):
        self.publish(env, published=False)
        assert self.post(env, OWNER).status_code == 404
        assert self.post(env, OUTSIDER).status_code == 404
        assert env.owners_seen == []

    def test_unknown_slug_is_404(self, env):
        self.publish(env)
        assert self.post(env, OUTSIDER, slug="autre").status_code == 404

    def test_authentication_is_required(self, env):
        self.publish(env)
        assert self.post(env, None).status_code == 401
        assert env.owners_seen == []

    def test_organization_visibility(self, env):
        self.publish(env, visibility=DashboardVisibility.ORGANIZATION)
        assert self.post(env, COLLEAGUE).status_code == 200
        assert self.post(env, OUTSIDER).status_code == 403

    def test_private_dashboard_is_served_to_its_owner_only(self, env):
        self.publish(env, visibility=DashboardVisibility.PRIVATE)
        assert self.post(env, OUTSIDER).status_code == 403
        assert self.post(env, COLLEAGUE).status_code == 403
        assert env.owners_seen == []  # nothing computed with the owner access
        assert self.post(env, OWNER).status_code == 200


class TestSpecPatch:
    ops = [
        {"op": "set_title", "title": "Pilotage v2"},
        {"op": "upsert_filter", "filter": {"id": "famille", "label": "Famille", "field": "famille"}},
        {"op": "add_visual", "component_id": "n1", "visual": {"type": "kpi", "source": "ventes", "kpi": {"measure": "n"}}},
    ]

    def fresh(self, env) -> Dashboard:
        return env.add(Dashboard(id="d1", title="Vide", created_by=OWNER, sources=[pipeline_source()], spec_version=2))

    def test_apply_ops_saves_and_returns_the_dashboard(self, env):
        self.fresh(env)
        resp = env.client(OWNER).post("/api/v1/dashboards/d1/spec", json={"ops": self.ops})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["title"] == "Pilotage v2" and body["spec_version"] == 2
        assert [c["id"] for c in body["components"]] == ["n1"]
        assert body["components"][0]["visual"]["type"] == "kpi"
        assert [f["id"] for f in body["filters"]] == ["famille"]
        saved = env.shared["d1"]
        assert saved.title == "Pilotage v2" and len(saved.components) == 1

    def test_invalid_spec_is_422_with_the_list_of_messages_and_nothing_saved(self, env):
        self.fresh(env)
        ops = [{"op": "add_visual", "visual": {"type": "kpi", "source": "fantome", "kpi": {"measure": "x"}}},
               {"op": "set_title", "title": "Ne sera pas appliqué"}]
        resp = env.client(OWNER).post("/api/v1/dashboards/d1/spec", json={"ops": ops})
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert isinstance(detail, list) and any("fantome" in m for m in detail)
        assert env.shared["d1"].title == "Vide" and env.shared["d1"].components == []

    def test_operation_error_message(self, env):
        self.fresh(env)
        resp = env.client(OWNER).post("/api/v1/dashboards/d1/spec", json={"ops": [{"op": "remove_component", "component_id": "zzz"}]})
        assert resp.status_code == 422 and "Opération 1" in resp.json()["detail"][0]

    def test_malformed_patch_body_is_422(self, env):
        self.fresh(env)
        assert env.client(OWNER).post("/api/v1/dashboards/d1/spec", json={"ops": [{"op": "inconnue"}]}).status_code == 422
        assert env.client(OWNER).post("/api/v1/dashboards/d1/spec", json={"ops": []}).status_code == 422

    def test_other_user_cannot_patch(self, env):
        self.fresh(env)
        resp = env.client(OUTSIDER).post("/api/v1/dashboards/d1/spec", json={"ops": self.ops})
        assert resp.status_code == 404
        assert env.shared["d1"].title == "Vide"

    def test_unknown_dashboard(self, env):
        assert env.client(OWNER).post("/api/v1/dashboards/nope/spec", json={"ops": self.ops}).status_code == 404

    def test_dry_run_returns_dashboard_and_data_without_saving(self, env):
        self.fresh(env)
        with env.patched():
            resp = env.client(OWNER).post("/api/v1/dashboards/d1/spec", json={"ops": self.ops, "dry_run": True})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert set(body) == {"dashboard", "data"}
        assert body["dashboard"]["title"] == "Pilotage v2"
        assert body["data"]["components"]["n1"]["value"]["value"] == 10 * 5  # 10 weeks x 5 families
        assert env.shared["d1"].title == "Vide" and env.shared["d1"].components == []

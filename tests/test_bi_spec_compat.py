"""Spec v2 does not change what v1 dashboards look like or how they load."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apowerb.bi.dashboards.core import (
    ChartWidget,
    ComponentType,
    Dashboard,
    DashboardComponent,
    GridPosition,
)
from apowerb.bi.dashboards.schema import DashboardCreateRequest, DashboardResponse
from apowerb.bi.dashboards.service import DashboardService, InMemoryDashboardStore
from apowerb.bi.spec import engine
from apowerb.bi.spec.errors import SpecError
from apowerb.bi.spec.models import DashboardDataRequest
from tests.helpers.bi_spec_data import build_dashboard, kpi

# A dashboard config exactly as stored before spec v2 (no spec_version / sources / filters).
LEGACY_CONFIG = {
    "id": "legacy-1",
    "title": "Ancien tableau",
    "description": "Avant la v2",
    "slug": "ancien",
    "components": [
        {
            "id": "c1",
            "component_type": "chart",
            "position": {"row": 0, "col": 0, "width": 8, "height": 4},
            "chart": {"chart_id": "chart-abc", "title_override": "Ventes", "refresh_interval_override": None},
            "key_value": None,
            "table": None,
        },
        {
            "id": "c2",
            "component_type": "key_value",
            "position": {"row": 0, "col": 8, "width": 4, "height": 2},
            "chart": None,
            "key_value": {"id": "kv1", "label": "Runs", "value": 2481, "unit": "runs", "description": None,
                          "trend": None, "source_query": None, "refresh_interval": 30},
            "table": None,
        },
        {
            "id": "c3",
            "component_type": "table",
            "position": {"row": 4, "col": 0, "width": 12, "height": 4},
            "chart": None,
            "key_value": None,
            "table": {"id": "t1", "title": "Commandes", "description": None, "source_query": "orders",
                      "columns": [{"key": "id", "label": "ID", "col_type": "string", "sortable": True,
                                   "filterable": False, "width": None, "format": None}],
                      "default_page_size": 25, "default_sort_field": None, "searchable": True, "refresh_interval": None},
        },
    ],
    "columns": 12,
    "status": "published",
    "visibility": "public",
    "version": 3,
    "permissions": [],
    "organization_id": "example.com",
    "project_id": "thaink2",
    "created_at": "2025-01-01T10:00:00Z",
    "updated_at": "2025-02-01T10:00:00Z",
    "created_by": "owner@example.com",
    "agent_id": 7,
}


class TestLegacyJsonLoads:
    def test_old_config_loads_with_v1_defaults(self):
        d = Dashboard(**LEGACY_CONFIG)
        assert d.spec_version == 1 and d.sources == [] and d.filters == []
        assert [c.component_type for c in d.components] == [ComponentType.CHART, ComponentType.KEY_VALUE, ComponentType.TABLE]
        assert all(c.visual is None for c in d.components)

    def test_legacy_fields_are_untouched_by_a_roundtrip(self):
        d = Dashboard(**LEGACY_CONFIG)
        dumped = json.loads(d.model_dump_json())
        for key in ("id", "title", "description", "slug", "columns", "status", "visibility", "version",
                    "permissions", "organization_id", "project_id", "created_by", "agent_id"):
            assert dumped[key] == LEGACY_CONFIG[key], key
        for original, back in zip(LEGACY_CONFIG["components"], dumped["components"]):
            for key, value in original.items():
                assert back[key] == value, (original["id"], key)
            assert back["visual"] is None  # the only addition
        assert Dashboard.model_validate_json(d.model_dump_json()) == d

    def test_row_loader_of_the_db_store(self):
        from apowerb.bi.db_stores import _dashboard_from_row

        row = MagicMock()
        row.config = dict(LEGACY_CONFIG)
        row.owner = "owner@example.com"
        d = _dashboard_from_row(row)
        assert d.spec_version == 1 and d.created_by == "owner@example.com"
        v2 = build_dashboard({"k": kpi("n")})
        row.config = json.loads(v2.model_dump_json())
        again = _dashboard_from_row(row)
        assert again.spec_version == 2 and again.sources[0].name == "ventes" and again.components[0].visual.type == "kpi"

    def test_chart_component_is_unchanged(self):
        comp = DashboardComponent.from_chart("chart-abc", position=GridPosition(row=1, col=2, width=5, height=3), title_override="T")
        assert comp.component_type == ComponentType.CHART
        assert comp.chart == ChartWidget(chart_id="chart-abc", title_override="T")
        assert comp.visual is None and comp.key_value is None and comp.table is None

    def test_exactly_one_field_rule_includes_visual(self):
        v = kpi("n")
        with pytest.raises(ValueError, match="Exactly one"):
            DashboardComponent(component_type=ComponentType.VISUAL)
        with pytest.raises(ValueError, match="Exactly one"):
            DashboardComponent(component_type=ComponentType.CHART, chart=ChartWidget(chart_id="x"), visual=v)
        with pytest.raises(ValueError, match="does not match"):
            DashboardComponent(component_type=ComponentType.CHART, visual=v)
        assert DashboardComponent(component_type=ComponentType.VISUAL, visual=v).visual == v

    def test_dashboard_stays_frozen_and_factory_signature_works(self):
        d = Dashboard.create("Titre", columns=12, permissions=["a"], created_by="o@example.com", agent_id=1)
        assert d.spec_version == 1
        with pytest.raises(Exception):
            d.title = "autre"  # type: ignore[misc]
        assert d.add_component(DashboardComponent.from_chart("c")).version == d.version + 1

    def test_response_schema_serialises_legacy_and_v2(self):
        legacy = DashboardResponse.from_domain(Dashboard(**LEGACY_CONFIG)).model_dump(mode="json")
        assert legacy["spec_version"] == 1 and legacy["sources"] == [] and legacy["filters"] == []
        assert legacy["components"][0]["chart"]["chart_id"] == "chart-abc"
        assert legacy["components"][0]["visual"] is None
        v2 = DashboardResponse.from_domain(build_dashboard({"k": kpi("n")})).model_dump(mode="json")
        assert v2["spec_version"] == 2 and v2["sources"][0]["name"] == "ventes" and v2["filters"]
        assert v2["components"][0]["visual"]["kpi"]["measure"] == "n"


class TestEngineOnLegacy:
    async def test_legacy_dashboard_computes_nothing_and_does_not_fail(self):
        res = await engine.compute_dashboard(Dashboard(**LEGACY_CONFIG), DashboardDataRequest(), owner="owner@example.com")
        assert res.components == {} and res.sources == {} and res.filters == {}


class TestServiceCreate:
    async def test_legacy_request_creates_a_v1_dashboard(self):
        svc = DashboardService(InMemoryDashboardStore())
        req = DashboardCreateRequest.model_validate({
            "title": "Ancien", "components": [{"component_type": "chart", "chart": {"chart_id": "abc"}}],
        })
        d = await svc.create(req, created_by="o@example.com")
        assert d.spec_version == 1 and d.sources == [] and d.components[0].chart.chart_id == "abc"

    async def test_v2_request_creates_a_v2_dashboard(self):
        svc = DashboardService(InMemoryDashboardStore())
        body = build_dashboard({"k": kpi("n")})
        req = DashboardCreateRequest.model_validate({
            "title": "Nouveau",
            "sources": [s.model_dump(mode="json") for s in body.sources],
            "filters": [f.model_dump(mode="json") for f in body.filters],
            "components": [{"component_type": "visual", "visual": body.components[0].visual.model_dump(mode="json")}],
        })
        d = await svc.create(req, created_by="o@example.com")
        assert d.spec_version == 2 and d.components[0].component_type == ComponentType.VISUAL

    async def test_inconsistent_v2_request_is_refused(self):
        svc = DashboardService(InMemoryDashboardStore())
        req = DashboardCreateRequest.model_validate({
            "title": "Cassé",
            "components": [{"component_type": "visual", "visual": {"type": "kpi", "source": "nope", "kpi": {"measure": "m"}}}],
        })
        with pytest.raises(SpecError):
            await svc.create(req, created_by="o@example.com")

    def test_rest_create_legacy_and_v2(self):
        from apowerb.auth.dependencies import get_current_user
        from apowerb.bi.dashboards.router import router
        from apowerb.bi.dependencies import get_dashboard_service

        store = InMemoryDashboardStore()
        app = FastAPI()
        app.include_router(router, prefix="/api/v1")
        user = MagicMock()
        user.email = "o@example.com"
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_dashboard_service] = lambda: DashboardService(store)
        client = TestClient(app)

        legacy = client.post("/api/v1/dashboards", json={"title": "Ancien", "components": [
            {"component_type": "chart", "chart": {"chart_id": "abc"}}]})
        assert legacy.status_code == 201 and legacy.json()["spec_version"] == 1

        body = build_dashboard({"k": kpi("n")})
        v2 = client.post("/api/v1/dashboards", json={
            "title": "Nouveau",
            "sources": [s.model_dump(mode="json") for s in body.sources],
            "components": [{"component_type": "visual", "visual": body.components[0].visual.model_dump(mode="json")}],
        })
        assert v2.status_code == 201, v2.text
        assert v2.json()["spec_version"] == 2 and v2.json()["components"][0]["visual"]["source"] == "ventes"
        added = client.post(f"/api/v1/dashboards/{v2.json()['id']}/components", json={"component": {
            "component_type": "visual", "visual": {"type": "kpi", "source": "ventes", "kpi": {"measure": "nsl"}}}})
        assert added.status_code == 201 and len(added.json()["components"]) == 2
        bad = client.post("/api/v1/dashboards", json={"title": "Cassé", "components": [
            {"component_type": "visual", "visual": {"type": "kpi", "source": "nope", "kpi": {"measure": "m"}}}]})
        assert bad.status_code == 422 and any("nope" in m for m in bad.json()["detail"])

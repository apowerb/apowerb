"""Unit tests for dashboard refresh scheduling endpoint."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apowerb.bi.dashboards.core import Dashboard, DashboardStatus, DashboardVisibility
from apowerb.bi.dashboards.service import DashboardNotFoundError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fake_dashboard(**overrides) -> Dashboard:
    defaults = dict(
        title="Test Dashboard",
        description="A test dashboard",
        slug="test-dashboard",
    )
    defaults.update(overrides)
    return Dashboard.create(**defaults)


def _fake_user():
    """Build a minimal user-like object for dependency injection."""
    user = MagicMock()
    user.email = "test@example.com"
    user.user_id = 1
    user.role = "USER"
    return user


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_dashboard_service():
    svc = AsyncMock()
    return svc


@pytest.fixture
def client(mock_dashboard_service):
    """Build a TestClient with mocked dependencies."""
    with patch("apowerb.bi.refresh_router.get_agent_by_id") as mock_get_agent, \
         patch("apowerb.bi.refresh_router.schedule_agent_run", new_callable=AsyncMock) as mock_schedule, \
         patch("apowerb.bi.refresh_router.get_orchestrator") as mock_orch, \
         patch("apowerb.bi.refresh_router.fetch_agents", return_value=[]) as mock_fetch, \
         patch("apowerb.bi.refresh_router.MageAPIClient") as mock_mage_client:

        from apowerb.bi.refresh_router import router
        from apowerb.auth.dependencies import get_current_user
        from apowerb.bi.dependencies import get_dashboard_service

        app = FastAPI()
        app.include_router(router, prefix="/api/v1")

        fake_user = _fake_user()

        async def override_get_current_user():
            return fake_user

        async def override_get_dashboard_service():
            return mock_dashboard_service

        app.dependency_overrides[get_current_user] = override_get_current_user
        app.dependency_overrides[get_dashboard_service] = override_get_dashboard_service

        test_client = TestClient(app)
        test_client._mock_svc = mock_dashboard_service
        test_client._mock_get_agent = mock_get_agent
        test_client._mock_schedule = mock_schedule
        test_client._mock_orch = mock_orch
        test_client._mock_fetch = mock_fetch
        test_client._mock_mage_client = mock_mage_client
        test_client._fake_user = fake_user

        yield test_client


# ---------------------------------------------------------------------------
# TestScheduleRefresh
# ---------------------------------------------------------------------------


class TestScheduleRefresh:
    def test_schedule_refresh_success(self, client):
        dashboard = _make_fake_dashboard()
        client._mock_svc.get = AsyncMock(return_value=dashboard)
        client._mock_get_agent.return_value = {"agent_id": "agent1"}
        client._mock_schedule.return_value = {
            "schedule_id": "sched-123",
            "message": "Scheduled successfully",
        }

        resp = client.post(
            f"/api/v1/dashboards/{dashboard.id}/schedule-refresh",
            json={"agent_id": 1, "interval": "@daily"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["dashboard_id"] == dashboard.id
        assert data["interval"] == "@daily"

    def test_schedule_refresh_dashboard_not_found(self, client):
        client._mock_svc.get = AsyncMock(side_effect=DashboardNotFoundError("fake-id"))

        resp = client.post(
            "/api/v1/dashboards/fake-id/schedule-refresh",
            json={"agent_id": 1, "interval": "@daily"},
        )
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

    def test_schedule_refresh_invalid_interval(self, client):
        dashboard = _make_fake_dashboard()
        client._mock_svc.get = AsyncMock(return_value=dashboard)
        client._mock_get_agent.return_value = {"agent_id": "agent1"}
        client._mock_schedule.side_effect = ValueError("Invalid schedule interval")

        resp = client.post(
            f"/api/v1/dashboards/{dashboard.id}/schedule-refresh",
            json={"agent_id": 1, "interval": "invalid-cron"},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# TestListSchedules
# ---------------------------------------------------------------------------


class TestListSchedules:
    def test_list_schedules(self, client):
        dashboard = _make_fake_dashboard()
        client._mock_svc.get = AsyncMock(return_value=dashboard)

        mock_orch_instance = MagicMock()
        mock_orch_instance.client.get_pipeline_schedules.return_value = [
            {
                "id": 1,
                "name": "agent1",
                "schedule_interval": "@daily",
                "status": "active",
                "next_pipeline_run_at": "2026-03-25T00:00:00Z",
                "created_at": "2026-03-24T00:00:00Z",
            }
        ]
        mock_orch_instance.PIPELINE_UUID = "test-pipeline"
        client._mock_orch.return_value = mock_orch_instance

        client._mock_fetch.return_value = [{"agent_id": 1}]

        resp = client.get(f"/api/v1/dashboards/{dashboard.id}/schedules")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)

    def test_list_schedules_empty(self, client):
        dashboard = _make_fake_dashboard()
        client._mock_svc.get = AsyncMock(return_value=dashboard)

        mock_orch_instance = MagicMock()
        mock_orch_instance.client.get_pipeline_schedules.return_value = []
        mock_orch_instance.PIPELINE_UUID = "test-pipeline"
        client._mock_orch.return_value = mock_orch_instance

        client._mock_fetch.return_value = []

        resp = client.get(f"/api/v1/dashboards/{dashboard.id}/schedules")
        assert resp.status_code == 200
        assert resp.json() == []


# ---------------------------------------------------------------------------
# TestDeleteSchedule
# ---------------------------------------------------------------------------


class TestDeleteSchedule:
    def test_delete_schedule(self, client):
        dashboard = _make_fake_dashboard()
        client._mock_svc.get = AsyncMock(return_value=dashboard)

        mock_response = MagicMock()
        mock_response.status_code = 204

        # The endpoint now verifies ownership before deleting:
        # 1. It fetches all schedules to find the target by ID
        # 2. It checks the schedule's name matches one of the user's agents
        mock_orch_instance = MagicMock()
        mock_orch_instance.client.base_url = "http://mage:6789"
        mock_orch_instance.client.project_name = "default"
        mock_orch_instance.client._get_headers.return_value = {}
        mock_orch_instance.PIPELINE_UUID = "pipe-uuid"
        # Return a schedule that matches sched-123 and belongs to agent1
        mock_orch_instance.client.get_pipeline_schedules.return_value = [
            {"id": "sched-123", "name": "agent1", "status": "active"},
        ]
        client._mock_orch.return_value = mock_orch_instance

        # fetch_agents must return an agent whose agent_id matches the schedule name
        client._mock_fetch.return_value = [{"agent_id": 1}]

        with patch("apowerb.bi.refresh_router.requests.delete") as mock_req_delete:
            mock_req_delete.return_value = mock_response

            resp = client.delete(
                f"/api/v1/dashboards/{dashboard.id}/schedules/sched-123"
            )
            assert resp.status_code == 204

    def test_delete_nonexistent(self, client):
        client._mock_svc.get = AsyncMock(side_effect=DashboardNotFoundError("fake-id"))

        resp = client.delete("/api/v1/dashboards/fake-id/schedules/sched-123")
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

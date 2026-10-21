"""Both orchestrator clients expose the same triggered-run response shape."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apowerb.auth.dependencies import get_current_user
from apowerb.scheduler.mage import MageAPIClient
from apowerb.scheduler.th2etl_client import Th2etlAPIClient


def test_triggered_run_has_the_same_shape_for_mage_and_th2etl():
    mage = MageAPIClient.__new__(MageAPIClient)
    mage.base_url = "http://mage.test:6789"
    mage.project_name = "agents"
    mage.api_key = "key"
    mage._ask = lambda send, **kwargs: {
        "pipeline_run": {"id": 7, "status": "pending"}
    }

    th2etl = Th2etlAPIClient.__new__(Th2etlAPIClient)
    th2etl._ask = lambda send, **kwargs: {
        "run_id": 7,
        "status": "pending",
    }

    results = [
        mage.trigger_pipeline_run_for_schedule(42),
        th2etl.trigger_pipeline_run_for_schedule("42"),
    ]

    for result in results:
        run = result["pipeline_run"]
        assert run["id"] == 7
        assert run["status"] == "pending"


def test_run_now_route_returns_the_normalized_run_fields():
    from apowerb.routers.adk_runner import router

    app = FastAPI()
    app.include_router(router, prefix="/api/adk")

    current_user = MagicMock(email="alice@example.com", user_id=1, role="USER")
    app.dependency_overrides[get_current_user] = lambda: current_user

    orchestrator = MagicMock()
    orchestrator.PIPELINE_UUID = "agents"
    orchestrator.client.get_pipeline_schedules.return_value = [
        {"name": "agent-1", "id": "schedule-42"}
    ]
    orchestrator.client.trigger_pipeline_run_for_schedule.return_value = {
        "pipeline_run": {"id": 7, "status": "pending"}
    }

    with (
        patch(
            "apowerb.scheduler.run_agent_background.get_agent_by_id",
            return_value={"agent_name": "agent-1"},
        ),
        patch(
            "apowerb.scheduler.run_agent_background.get_orchestrator",
            return_value=orchestrator,
        ),
        patch(
            "apowerb.scheduler.run_agent_background.create_agent_run_token",
            return_value="test-jwt",
        ),
        patch("apowerb.core.agent_main.get_agent_folder_name", return_value="agent-1"),
    ):
        response = TestClient(app).post(
            "/api/adk/run_now",
            json={
                "agent_id": "agent-1",
                "user_id": "alice@example.com",
                "message": "run it",
            },
        )

    assert response.status_code == 200
    assert response.json()["run_id"] == 7
    assert response.json()["status"] == "pending"

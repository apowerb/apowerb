"""Robust error handling for chart data fetching.

Two prod symptoms: dashboards froze for 15 min on a hung agent, and an agent
returning non-JSON produced a silently empty chart (no error). Now the agent
executor surfaces unparseable output, the service wraps any executor failure in
QueryExecutionError, and the router maps it to a 502 (not a bare 500).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# AgentQueryExecutor._parse_response
# ---------------------------------------------------------------------------


class TestAgentParseResponse:
    def _parse(self, result):
        from th2agent.bi.data.agent_executor import AgentQueryExecutor

        return AgentQueryExecutor._parse_response(result)

    def test_empty_text_returns_empty_list(self):
        assert self._parse({"response": ""}) == []
        assert self._parse([]) == []

    def test_parses_json_array(self):
        rows = self._parse({"response": '[{"a": 1}, {"a": 2}]'})
        assert rows == [{"a": 1}, {"a": 2}]

    def test_parses_fenced_json(self):
        rows = self._parse({"response": "```json\n[{\"x\": 9}]\n```"})
        assert rows == [{"x": 9}]

    def test_raises_on_non_json_text(self):
        # Text present but not JSON rows → must surface, not silently return [].
        with pytest.raises(ValueError):
            self._parse({"response": "Sorry, I could not run that query."})


# ---------------------------------------------------------------------------
# Router maps QueryExecutionError -> 502
# ---------------------------------------------------------------------------


def _build_app():
    from th2agent.bi.data.router import router as data_router
    from th2agent.helpers.database import get_db

    app = FastAPI()
    app.include_router(data_router, prefix="/api/v1")

    async def fake_db():
        yield AsyncMock()

    app.dependency_overrides[get_db] = fake_db
    return app


class TestRouterMapsExecutionError:
    def test_public_endpoint_returns_502_on_query_execution_error(self):
        from th2agent.bi.data.service import QueryExecutionError

        app = _build_app()
        fake_chart = MagicMock()
        fake_chart.id = "chart1"
        fake_chart.created_by = "owner@example.com"

        mock_get = AsyncMock(return_value=fake_chart)
        mock_fetch = AsyncMock(
            side_effect=QueryExecutionError("chart1", "agent", RuntimeError("boom"))
        )

        with patch("th2agent.bi.charts.service.ChartService") as MockChartSvc, patch(
            "th2agent.bi.data.service.ChartDataService"
        ) as MockDataSvc:
            MockChartSvc.return_value.get = mock_get
            MockDataSvc.return_value.fetch = mock_fetch

            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/api/v1/public/charts/chart1/data")

        assert resp.status_code == 502, resp.text
        # The raw cause must stay in server logs, never echoed to the client.
        assert "boom" not in resp.text


class TestAuthRouteNoLeak:
    """The authenticated /charts/{id}/data must also not leak the raw cause."""

    def test_authenticated_endpoint_returns_502_without_leaking_cause(self):
        from th2agent.bi.data.router import router as data_router
        from th2agent.bi.data.service import QueryExecutionError
        from th2agent.bi.dependencies import get_data_service
        from th2agent.auth.dependencies import get_current_user
        from th2agent.helpers.database import get_db

        app = FastAPI()
        app.include_router(data_router, prefix="/api/v1")

        async def fake_db():
            yield AsyncMock()

        async def fake_user():
            u = MagicMock()
            u.email = "owner@example.com"
            return u

        svc = MagicMock()
        svc.fetch = AsyncMock(
            side_effect=QueryExecutionError("chart1", "agent", RuntimeError("boom"))
        )

        app.dependency_overrides[get_db] = fake_db
        app.dependency_overrides[get_current_user] = fake_user
        app.dependency_overrides[get_data_service] = lambda: svc

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/v1/charts/chart1/data")

        assert resp.status_code == 502, resp.text
        assert "boom" not in resp.text

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
        from apowerb.bi.data.agent_executor import AgentQueryExecutor

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


class TestAgentParseGeminiEvents:
    """ADK events carry Gemini 2.5 *thought* parts alongside the answer.

    Prod symptom (08/09, chart 0db64b90): the executor took the FIRST part
    holding text, which for a thinking model is the reasoning summary
    ("**My Current Line of Reasoning**..."). The real answer sitting in the
    next part was never looked at, and the chart 502'd on an agent that had
    in fact answered.
    """

    def _parse(self, result):
        from apowerb.bi.data.agent_executor import AgentQueryExecutor

        return AgentQueryExecutor._parse_response(result)

    def test_thought_part_is_not_the_answer(self):
        events = [
            {
                "modelVersion": "gemini-2.5-flash",
                "content": {
                    "parts": [
                        {
                            "text": "**My Current Line of Reasoning**\n\nOkay, so...",
                            "thought": True,
                        },
                        {"text": '[{"region": "EU", "total": 3}]'},
                    ]
                },
            }
        ]
        assert self._parse(events) == [{"region": "EU", "total": 3}]

    def test_answer_in_earlier_event_is_still_found(self):
        # The final event is a thought-only wrap-up; the rows came before it.
        events = [
            {"content": {"parts": [{"text": '[{"a": 1}]'}]}},
            {"content": {"parts": [{"text": "Reflecting on it.", "thought": True}]}},
        ]
        assert self._parse(events) == [{"a": 1}]

    def test_fenced_json_inside_an_event(self):
        events = [
            {
                "content": {
                    "parts": [{"text": 'Here you go:\n```json\n[{"x": 9}]\n```'}]
                }
            }
        ]
        assert self._parse(events) == [{"x": 9}]

    def test_only_thoughts_reports_the_missing_instruction(self):
        events = [
            {
                "content": {
                    "parts": [
                        {"text": "I do not know what data is wanted.", "thought": True}
                    ]
                }
            }
        ]
        with pytest.raises(ValueError) as exc:
            self._parse(events)
        # The message must point at the fixable cause, not just quote the model.
        assert "source_options" in str(exc.value)

    def test_error_names_the_agent_when_known(self):
        from apowerb.bi.data.agent_executor import AgentQueryExecutor

        with pytest.raises(ValueError) as exc:
            AgentQueryExecutor._parse_response(
                {"response": "Sorry, I could not run that query."},
                agent_name="sales_bot",
            )
        assert "sales_bot" in str(exc.value)


class TestAgentPromptContract:
    """Without an instruction the agent is asked for "the latest data" -- a
    question no agent can answer. The fallback must at least state the output
    contract, and an instruction carried by the chart must win over it."""

    def _message(self, source):
        from apowerb.bi.data.agent_executor import AgentQueryExecutor

        return AgentQueryExecutor._message_for(source)

    def test_source_options_message_wins(self):
        from apowerb.bi.charts.core import DataSource, SourceType

        src = DataSource(
            source_type=SourceType.AGENT,
            query="",
            source_options={"agent_ids": [1], "message": "Monthly revenue by region"},
        )
        assert self._message(src) == "Monthly revenue by region"

    def test_query_used_when_no_message(self):
        from apowerb.bi.charts.core import DataSource, SourceType

        src = DataSource(
            source_type=SourceType.AGENT,
            query="Top 10 clients",
            source_options={"agent_ids": [1]},
        )
        assert self._message(src) == "Top 10 clients"

    def test_fallback_states_the_json_only_contract(self):
        from apowerb.bi.charts.core import DataSource, SourceType

        src = DataSource(
            source_type=SourceType.AGENT, query="", source_options={"agent_ids": [1]}
        )
        msg = self._message(src)
        assert "JSON" in msg
        assert "only" in msg.lower()


# ---------------------------------------------------------------------------
# Router maps QueryExecutionError -> 502
# ---------------------------------------------------------------------------


def _build_app():
    from apowerb.bi.data.router import router as data_router
    from apowerb.helpers.database import get_db

    app = FastAPI()
    app.include_router(data_router, prefix="/api/v1")

    async def fake_db():
        yield AsyncMock()

    app.dependency_overrides[get_db] = fake_db
    return app


class TestRouterMapsExecutionError:
    def test_public_endpoint_returns_502_on_query_execution_error(self):
        from apowerb.bi.data.service import QueryExecutionError

        app = _build_app()
        fake_chart = MagicMock()
        fake_chart.id = "chart1"
        fake_chart.created_by = "owner@example.com"

        mock_get = AsyncMock(return_value=fake_chart)
        mock_fetch = AsyncMock(
            side_effect=QueryExecutionError("chart1", "agent", RuntimeError("boom"))
        )

        from apowerb.auth.dependencies import get_current_user

        app.dependency_overrides[get_current_user] = lambda: MagicMock(email="owner@example.com")
        with patch(
            "apowerb.bi.data.router._stored_chart_owner",
            AsyncMock(return_value="owner@example.com"),
        ), patch("apowerb.bi.charts.service.ChartService") as MockChartSvc, patch(
            "apowerb.bi.data.service.ChartDataService"
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
        from apowerb.bi.data.router import router as data_router
        from apowerb.bi.data.service import QueryExecutionError
        from apowerb.bi.dependencies import get_data_service
        from apowerb.auth.dependencies import get_current_user
        from apowerb.helpers.database import get_db

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

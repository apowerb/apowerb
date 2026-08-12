"""Integration tests for the two new evaluations routes:

- `GET /evaluations/agents` -- one row per owned agent, evaluated or not.
  Never-evaluated agents (`last_run: null`) sort first; a bounded number
  of queries regardless of how many agents are returned (no N+1).
- `GET /evaluations/runs?agent_id=` -- one agent's run history, most
  recent first, each run carrying all of its results.

Same harness as tests/test_evaluations_router.py: a standalone FastAPI app
with just this router mounted, `get_current_user` / `get_db` overridden,
`_FakeSession` replaying queued `db.execute()` results in call order.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient

from apowerb.evaluation.models import EvaluationResult


def _fake_user(email="me@example.com", role="user"):
    u = MagicMock()
    u.email = email
    u.role = role
    return u


def _eval_row(**overrides):
    defaults = dict(
        id=1,
        created_at=datetime(2026, 8, 10, 11, 4, 12, tzinfo=timezone.utc),
        agent_id=1234,
        run_id=uuid.UUID("11111111-1111-4111-8111-111111111111"),
        session_id="session_1786030573591",
        evaluator_name="tool_execution_outcome",
        evaluator_kind="deterministic",
        judge_model=None,
        score=0.6667,
        passed=False,
        details={"tool_calls": 3},
    )
    defaults.update(overrides)
    row = MagicMock(spec=EvaluationResult)
    for key, value in defaults.items():
        setattr(row, key, value)
    return row


class _FakeSession:
    """Returns queued results for successive `execute()` calls, in order."""

    def __init__(self, results):
        self._results = list(results)
        self.executed_stmts: list = []

    async def execute(self, stmt, params=None):
        self.executed_stmts.append(stmt)
        return self._results.pop(0)


def _all(rows):
    res = MagicMock()
    res.all = MagicMock(return_value=rows)
    return res


def _scalar_one(value):
    res = MagicMock()
    res.scalar_one = MagicMock(return_value=value)
    return res


def _scalars_all(rows):
    res = MagicMock()
    res.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=rows)))
    return res


def _agent_count_row(agent_id, runs_count):
    return MagicMock(agent_id=agent_id, runs_count=runs_count)


def _last_run_row(agent_id, run_id):
    return MagicMock(agent_id=agent_id, run_id=run_id)


def _run_page_row(run_id, created_at):
    return MagicMock(run_id=run_id, created_at=created_at)


def _build_app(
    session=None,
    *,
    user=None,
    evaluation_enabled=True,
):
    from apowerb.auth.dependencies import get_current_user
    from apowerb.helpers.database import get_db
    from apowerb.routers.evaluations import router

    app = FastAPI()
    app.include_router(router, prefix="/api")

    async def _user_override():
        if user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
        return user

    async def _db_override():
        yield session

    app.dependency_overrides[get_current_user] = _user_override
    app.dependency_overrides[get_db] = _db_override

    fake_settings = MagicMock()
    fake_settings.evaluation_enabled = evaluation_enabled
    patcher = patch("apowerb.routers.evaluations.get_settings", return_value=fake_settings)
    patcher.start()
    app.state._settings_patcher = patcher
    return app


# ---------------------------------------------------------------------------
# GET /evaluations/agents
# ---------------------------------------------------------------------------


class TestListAgentsEvaluationState:
    def test_disabled_feature_is_404(self):
        app = _build_app(user=_fake_user(), evaluation_enabled=False)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/api/evaluations/agents")

        assert resp.status_code == 404

    def test_owner_with_no_agents_gets_empty_list(self):
        session = _FakeSession([])
        app = _build_app(session=session, user=_fake_user())
        client = TestClient(app)

        with patch(
            "apowerb.routers.evaluations.list_owned_agents",
            new=AsyncMock(return_value=[]),
        ):
            resp = client.get("/api/evaluations/agents")

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"items": []}
        # Zero owned agents: no evaluation-results query at all.
        assert session.executed_stmts == []

    def test_never_evaluated_agent_has_null_last_run_and_sorts_first(self):
        session = _FakeSession(
            [
                _all([_agent_count_row(164, 12)]),  # counts
                _all([_last_run_row(164, uuid.UUID("22222222-2222-4222-8222-222222222222"))]),  # last run per agent
                _scalars_all(
                    [_eval_row(agent_id=164, run_id=uuid.UUID("22222222-2222-4222-8222-222222222222"))]
                ),  # full rows of that run
            ]
        )
        app = _build_app(session=session, user=_fake_user())
        client = TestClient(app)

        with patch(
            "apowerb.routers.evaluations.list_owned_agents",
            new=AsyncMock(
                return_value=[
                    (164, "Send_mail", "owner@example.com"),
                    (200, "Never_run", "owner@example.com"),
                ]
            ),
        ):
            resp = client.get("/api/evaluations/agents")

        assert resp.status_code == 200, resp.text
        items = resp.json()["items"]
        assert len(items) == 2
        # Never-evaluated agent (200) sorts before the evaluated one (164).
        assert items[0]["agent_id"] == 200
        assert items[0]["last_run"] is None
        assert items[0]["runs_count"] == 0
        assert items[1]["agent_id"] == 164
        assert items[1]["runs_count"] == 12

    def test_last_run_carries_all_results_of_that_run(self):
        run_id = uuid.uuid4()
        rows = [
            _eval_row(agent_id=164, run_id=run_id, evaluator_name="tool_execution_outcome"),
            _eval_row(agent_id=164, run_id=run_id, evaluator_name="task_completion_judge"),
            _eval_row(agent_id=164, run_id=run_id, evaluator_name="tool_usage"),
            _eval_row(agent_id=164, run_id=run_id, evaluator_name="coherence"),
            _eval_row(agent_id=164, run_id=run_id, evaluator_name="completeness"),
            _eval_row(agent_id=164, run_id=run_id, evaluator_name="hallucination"),
        ]
        session = _FakeSession(
            [
                _all([_agent_count_row(164, 1)]),
                _all([_last_run_row(164, run_id)]),
                _scalars_all(rows),
            ]
        )
        app = _build_app(session=session, user=_fake_user())
        client = TestClient(app)

        with patch(
            "apowerb.routers.evaluations.list_owned_agents",
            new=AsyncMock(return_value=[(164, "Send_mail", "owner@example.com")]),
        ):
            resp = client.get("/api/evaluations/agents")

        item = resp.json()["items"][0]
        assert len(item["last_run"]["results"]) == 6
        assert item["last_run"]["run_id"] == str(run_id)
        assert item["last_run"]["session_id"] == "session_1786030573591"

    def test_not_applicable_result_has_null_score_not_zero(self):
        run_id = uuid.uuid4()
        row = _eval_row(
            agent_id=164,
            run_id=run_id,
            evaluator_name="hallucination",
            evaluator_kind="llm_judge",
            score=None,
            passed=None,
            details={"not_applicable": "grounding unavailable"},
        )
        session = _FakeSession(
            [
                _all([_agent_count_row(164, 1)]),
                _all([_last_run_row(164, run_id)]),
                _scalars_all([row]),
            ]
        )
        app = _build_app(session=session, user=_fake_user())
        client = TestClient(app)

        with patch(
            "apowerb.routers.evaluations.list_owned_agents",
            new=AsyncMock(return_value=[(164, "Send_mail", "owner@example.com")]),
        ):
            resp = client.get("/api/evaluations/agents")

        result = resp.json()["items"][0]["last_run"]["results"][0]
        assert result["score"] is None
        assert result["passed"] is None
        assert result["not_applicable"] == "grounding unavailable"

    def test_query_count_does_not_grow_with_number_of_agents(self):
        """N+1 guard: 20 owned agents still cost exactly 3 evaluation queries."""
        agents = [(i, f"agent_{i}", "owner@example.com") for i in range(20)]
        run_id = uuid.uuid4()
        session = _FakeSession(
            [
                _all([_agent_count_row(0, 1)]),
                _all([_last_run_row(0, run_id)]),
                _scalars_all([_eval_row(agent_id=0, run_id=run_id)]),
            ]
        )
        app = _build_app(session=session, user=_fake_user())
        client = TestClient(app)

        with patch(
            "apowerb.routers.evaluations.list_owned_agents",
            new=AsyncMock(return_value=agents),
        ):
            resp = client.get("/api/evaluations/agents")

        assert resp.status_code == 200, resp.text
        assert len(session.executed_stmts) == 3


# ---------------------------------------------------------------------------
# GET /evaluations/runs
# ---------------------------------------------------------------------------


class TestListEvaluationRuns:
    def test_disabled_feature_is_404(self):
        app = _build_app(user=_fake_user(), evaluation_enabled=False)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/api/evaluations/runs?agent_id=164")

        assert resp.status_code == 404

    def test_requesting_an_agent_you_do_not_own_is_403(self):
        session = _FakeSession([])
        app = _build_app(session=session, user=_fake_user())
        client = TestClient(app, raise_server_exceptions=False)

        with patch(
            "apowerb.routers.evaluations.owned_agent_ids",
            new=AsyncMock(return_value={1234}),
        ):
            resp = client.get("/api/evaluations/runs?agent_id=9999")

        assert resp.status_code == 403

    def test_runs_are_ordered_most_recent_first_with_all_results(self):
        agent_id = 164
        newer_run = uuid.uuid4()
        older_run = uuid.uuid4()
        newer_time = datetime(2026, 8, 11, 15, 33, 35, tzinfo=timezone.utc)
        older_time = newer_time - timedelta(days=1)

        session = _FakeSession(
            [
                _scalar_one(2),  # total distinct runs
                _all(
                    [
                        _run_page_row(newer_run, newer_time),
                        _run_page_row(older_run, older_time),
                    ]
                ),
                _scalars_all(
                    [
                        _eval_row(agent_id=agent_id, run_id=newer_run, created_at=newer_time),
                        _eval_row(agent_id=agent_id, run_id=older_run, created_at=older_time),
                    ]
                ),
            ]
        )
        app = _build_app(session=session, user=_fake_user())
        client = TestClient(app)

        with patch(
            "apowerb.routers.evaluations.owned_agent_ids",
            new=AsyncMock(return_value={agent_id}),
        ):
            resp = client.get(f"/api/evaluations/runs?agent_id={agent_id}")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] == 2
        assert [item["run_id"] for item in body["items"]] == [str(newer_run), str(older_run)]

    def test_default_pagination_is_20(self):
        session = _FakeSession([_scalar_one(0), _all([])])
        app = _build_app(session=session, user=_fake_user())
        client = TestClient(app)

        with patch(
            "apowerb.routers.evaluations.owned_agent_ids",
            new=AsyncMock(return_value={164}),
        ):
            resp = client.get("/api/evaluations/runs?agent_id=164")

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"items": [], "total": 0}
        # No results query at all when the run page is empty.
        assert len(session.executed_stmts) == 2

    def test_admin_can_view_any_agents_runs(self):
        session = _FakeSession([_scalar_one(0), _all([])])
        app = _build_app(session=session, user=_fake_user(role="admin"))
        client = TestClient(app)

        with patch(
            "apowerb.routers.evaluations.owned_agent_ids",
            new=AsyncMock(return_value=None),
        ):
            resp = client.get("/api/evaluations/runs?agent_id=99999")

        assert resp.status_code == 200, resp.text

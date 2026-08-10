"""Integration tests for routers/evaluations.py.

Mirrors tests/test_notifications_router.py: a standalone FastAPI app with
just this router mounted, `get_current_user` / `get_db` overridden. Covers
the three contract-mandated properties this router (not run_service.py) is
responsible for:

- the feature flag gates all three routes with 404, checked before auth;
- GET endpoints enforce ownership by building the query with an owned-agent
  filter, never by trimming an already-fetched list;
- the response shape matches the contract exactly, including `applicable`
  and null (not zero) pass_rate/avg_score when nothing was applicable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient

from apowerb.evaluation.models import EvaluationResult
from apowerb.evaluation.run_service import SessionContext


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


def _scalar_one(value):
    res = MagicMock()
    res.scalar_one = MagicMock(return_value=value)
    return res


def _scalars_all(rows):
    res = MagicMock()
    res.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=rows)))
    return res


def _all(rows):
    res = MagicMock()
    res.all = MagicMock(return_value=rows)
    return res


def _build_app(session=None, *, user=None, evaluation_enabled=True):
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
    app.state._settings_patcher = patcher  # keep alive for the test's lifetime
    return app


# ---------------------------------------------------------------------------
# Feature flag gate
# ---------------------------------------------------------------------------


class TestFeatureFlagGate:
    def test_disabled_feature_is_404_even_when_unauthenticated(self):
        app = _build_app(user=None, evaluation_enabled=False)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/api/evaluations")

        assert resp.status_code == 404

    def test_disabled_feature_is_404_on_run(self):
        app = _build_app(user=_fake_user(), evaluation_enabled=False)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.post("/api/evaluations/run", json={"session_id": "session_x"})

        assert resp.status_code == 404

    def test_enabled_feature_requires_auth(self):
        app = _build_app(user=None, evaluation_enabled=True)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/api/evaluations")

        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /evaluations/run
# ---------------------------------------------------------------------------


class TestRunEvaluation:
    def test_run_returns_the_contract_shape(self):
        row = _eval_row()
        app = _build_app(session=_FakeSession([]), user=_fake_user())
        client = TestClient(app)

        ctx = SessionContext(
            agent_id=1234, app_name="agent1234", session_user_id="me@example.com",
            judged_model="gemini/gemini-2.5-flash", owner_id="me@example.com",
        )
        with (
            patch(
                "apowerb.routers.evaluations.resolve_session_context",
                new=AsyncMock(return_value=ctx),
            ),
            patch(
                "apowerb.routers.evaluations.run_and_persist",
                new=AsyncMock(return_value=[row]),
            ),
        ):
            resp = client.post(
                "/api/evaluations/run", json={"session_id": "session_1786030573591"}
            )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["session_id"] == "session_1786030573591"
        assert len(body["results"]) == 1
        result = body["results"][0]
        assert result["score"] == 0.6667
        assert result["applicable"] is True
        assert result["details"] == {"tool_calls": 3}

    def test_run_on_a_session_not_owned_is_403(self):
        app = _build_app(session=_FakeSession([]), user=_fake_user())
        client = TestClient(app, raise_server_exceptions=False)

        with patch(
            "apowerb.routers.evaluations.resolve_session_context",
            new=AsyncMock(side_effect=HTTPException(status_code=403, detail="Not your agent")),
        ):
            resp = client.post("/api/evaluations/run", json={"session_id": "session_x"})

        assert resp.status_code == 403

    def test_not_applicable_result_has_null_score_and_passed(self):
        row = _eval_row(
            evaluator_name="task_completion_judge",
            evaluator_kind="llm_judge",
            score=None,
            passed=None,
            details={"not_applicable": "the session has no transcript to judge"},
        )
        app = _build_app(session=_FakeSession([]), user=_fake_user())
        client = TestClient(app)

        ctx = SessionContext(
            agent_id=1234, app_name="agent1234", session_user_id="me@example.com",
            judged_model="gemini/gemini-2.5-flash", owner_id="me@example.com",
        )
        with (
            patch(
                "apowerb.routers.evaluations.resolve_session_context",
                new=AsyncMock(return_value=ctx),
            ),
            patch(
                "apowerb.routers.evaluations.run_and_persist",
                new=AsyncMock(return_value=[row]),
            ),
        ):
            resp = client.post("/api/evaluations/run", json={"session_id": "session_x"})

        result = resp.json()["results"][0]
        assert result["score"] is None
        assert result["passed"] is None
        assert result["applicable"] is False


# ---------------------------------------------------------------------------
# GET /evaluations
# ---------------------------------------------------------------------------


class TestListEvaluations:
    def test_requesting_an_agent_you_do_not_own_is_403(self):
        session = _FakeSession([])
        app = _build_app(session=session, user=_fake_user())
        client = TestClient(app, raise_server_exceptions=False)

        with patch(
            "apowerb.routers.evaluations.owned_agent_ids",
            new=AsyncMock(return_value={1234}),
        ):
            resp = client.get("/api/evaluations?agent_id=9999")

        assert resp.status_code == 403

    def test_owner_with_zero_agents_gets_an_empty_list_not_an_error(self):
        session = _FakeSession([])
        app = _build_app(session=session, user=_fake_user())
        client = TestClient(app)

        with patch(
            "apowerb.routers.evaluations.owned_agent_ids",
            new=AsyncMock(return_value=set()),
        ):
            resp = client.get("/api/evaluations")

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"items": [], "total": 0}

    def test_admin_lists_across_owners(self):
        row = _eval_row(agent_id=42)
        session = _FakeSession([_scalar_one(1), _scalars_all([row])])
        app = _build_app(session=session, user=_fake_user(role="admin"))
        client = TestClient(app)

        with patch(
            "apowerb.routers.evaluations.owned_agent_ids",
            new=AsyncMock(return_value=None),
        ):
            resp = client.get("/api/evaluations")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["agent_id"] == 42
        # An admin's query must not carry an agent_id IN (...) restriction.
        assert "agent_evaluation_results.agent_id IN" not in str(session.executed_stmts[0])


# ---------------------------------------------------------------------------
# GET /evaluations/summary
# ---------------------------------------------------------------------------


class TestEvaluationsSummary:
    def test_zero_applicable_runs_gives_null_not_zero(self):
        agg_row = MagicMock(
            evaluator_name="tool_execution_outcome",
            evaluator_kind="deterministic",
            runs=5,
            applicable_runs=0,
            passed=0,
            avg_score=None,
        )
        session = _FakeSession([_all([agg_row])])
        app = _build_app(session=session, user=_fake_user())
        client = TestClient(app)

        with patch(
            "apowerb.routers.evaluations.owned_agent_ids",
            new=AsyncMock(return_value={1234}),
        ):
            resp = client.get("/api/evaluations/summary")

        assert resp.status_code == 200, resp.text
        summary = resp.json()["by_evaluator"][0]
        assert summary["runs"] == 5
        assert summary["applicable_runs"] == 0
        assert summary["pass_rate"] is None
        assert summary["avg_score"] is None

    def test_applicable_runs_computes_pass_rate_and_avg_score(self):
        agg_row = MagicMock(
            evaluator_name="tool_execution_outcome",
            evaluator_kind="deterministic",
            runs=12,
            applicable_runs=9,
            passed=7,
            avg_score=0.81344,
        )
        session = _FakeSession([_all([agg_row])])
        app = _build_app(session=session, user=_fake_user())
        client = TestClient(app)

        with patch(
            "apowerb.routers.evaluations.owned_agent_ids",
            new=AsyncMock(return_value={1234}),
        ):
            resp = client.get("/api/evaluations/summary")

        summary = resp.json()["by_evaluator"][0]
        assert summary["pass_rate"] == round(7 / 9, 4)
        assert summary["avg_score"] == 0.8134

    def test_owner_with_zero_agents_gets_an_empty_summary(self):
        session = _FakeSession([])
        app = _build_app(session=session, user=_fake_user())
        client = TestClient(app)

        with patch(
            "apowerb.routers.evaluations.owned_agent_ids",
            new=AsyncMock(return_value=set()),
        ):
            resp = client.get("/api/evaluations/summary")

        assert resp.status_code == 200, resp.text
        assert resp.json()["by_evaluator"] == []

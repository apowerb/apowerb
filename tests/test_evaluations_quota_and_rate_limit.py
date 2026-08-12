"""Audit fix, point 5 (GRAVE): evaluations had no quota and no rate limit.

`POST /evaluations/run` is the only LLM-spending route that never called
`core.run_gate.apply_run_guards` -- the single choke point every other
entry door (chat, scheduled runs, webhooks) goes through (see
`tests/test_run_gate_couverture.py`). Any authenticated user could loop
evaluations, billed to thaink2 by default, without ever touching a quota.

Two independent fixes, tested separately:
- the existing commercial quota system is wired in through the same
  `apply_run_guards` choke point everything else uses;
- a same-process rate limit blocks an immediate re-run of the SAME
  session, independent of any commercial extension being installed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from apowerb.core.extensions.registry import registry
from apowerb.evaluation.run_service import SessionContext


def _fake_user(email="me@example.com", role="user"):
    u = MagicMock()
    u.email = email
    u.role = role
    return u


def _build_app(*, user=None, evaluation_enabled=True):
    from apowerb.auth.dependencies import get_current_user
    from apowerb.helpers.database import get_db
    from apowerb.routers.evaluations import router

    app = FastAPI()
    app.include_router(router, prefix="/api")

    async def _user_override():
        if user is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        return user

    async def _db_override():
        yield MagicMock()

    app.dependency_overrides[get_current_user] = _user_override
    app.dependency_overrides[get_db] = _db_override

    fake_settings = MagicMock()
    fake_settings.evaluation_enabled = evaluation_enabled
    fake_settings.evaluation_judge_model = "gemini/gemini-2.5-flash"
    fake_settings.evaluation_judge_api_key = "server-key"
    patcher = patch("apowerb.routers.evaluations.get_settings", return_value=fake_settings)
    patcher.start()
    app.state._settings_patcher = patcher
    return app


@pytest.fixture
def registre_vierge(monkeypatch):
    """Isole les gardes de quota : un test ne doit pas voir celles d'un autre."""
    monkeypatch.setattr(registry, "_run_guards", [], raising=False)
    return registry


@pytest.fixture(autouse=True)
def rate_limit_vierge(monkeypatch):
    """Isole l'etat du garde-fou de frequence entre les tests."""
    from apowerb.evaluation import run_service

    monkeypatch.setattr(run_service, "_last_run_at", {}, raising=False)


def _ctx(owner_id="me@example.com", app_name="agent1234"):
    return SessionContext(
        agent_id=1234, app_name=app_name, session_user_id=owner_id,
        judged_model="gemini/gemini-2.5-flash", owner_id=owner_id,
    )


class TestQuotaGuardWiring:
    def test_no_guard_installed_nothing_blocks(self, registre_vierge):
        app = _build_app(user=_fake_user())
        client = TestClient(app)

        with (
            patch(
                "apowerb.routers.evaluations.resolve_session_context",
                new=AsyncMock(return_value=_ctx()),
            ),
            patch(
                "apowerb.routers.evaluations.run_and_persist",
                new=AsyncMock(return_value=[]),
            ),
        ):
            resp = client.post(
                "/api/evaluations/run", json={"session_id": "session_x"}
            )

        assert resp.status_code == 200, resp.text

    def test_quota_guard_refusal_blocks_the_run_and_never_calls_run_and_persist(
        self, registre_vierge
    ):
        async def garde(agent_name, *, owner_id, plan):
            raise HTTPException(status_code=402, detail={"code": "QUOTA_EXCEEDED"})

        registre_vierge.register_run_guard(garde)

        app = _build_app(user=_fake_user())
        client = TestClient(app, raise_server_exceptions=False)

        with (
            patch(
                "apowerb.routers.evaluations.resolve_session_context",
                new=AsyncMock(return_value=_ctx()),
            ),
            patch(
                "apowerb.routers.evaluations.run_and_persist", new=AsyncMock()
            ) as run_mock,
        ):
            resp = client.post(
                "/api/evaluations/run", json={"session_id": "session_x"}
            )

        assert resp.status_code == 402
        run_mock.assert_not_called()

    def test_guard_receives_the_resolved_agent_owner_and_plan(self, registre_vierge):
        vus: list[tuple] = []

        async def garde(agent_name, *, owner_id, plan):
            vus.append((agent_name, owner_id, plan))

        registre_vierge.register_run_guard(garde)

        app = _build_app(user=_fake_user())
        client = TestClient(app)

        with (
            patch(
                "apowerb.routers.evaluations.resolve_session_context",
                new=AsyncMock(return_value=_ctx(owner_id="me@example.com", app_name="agent7")),
            ),
            patch(
                "apowerb.routers.evaluations.run_and_persist",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "apowerb.routers.evaluations.resolve_owner_plan",
                new=AsyncMock(return_value="pro"),
            ),
        ):
            client.post("/api/evaluations/run", json={"session_id": "session_x"})

        assert vus == [("agent7", "me@example.com", "pro")]


class TestRerunRateLimit:
    def test_second_run_of_the_same_session_within_the_window_is_429(
        self, registre_vierge
    ):
        app = _build_app(user=_fake_user())
        client = TestClient(app, raise_server_exceptions=False)

        with (
            patch(
                "apowerb.routers.evaluations.resolve_session_context",
                new=AsyncMock(return_value=_ctx()),
            ),
            patch(
                "apowerb.routers.evaluations.run_and_persist",
                new=AsyncMock(return_value=[]),
            ) as run_mock,
        ):
            first = client.post(
                "/api/evaluations/run", json={"session_id": "session_x"}
            )
            second = client.post(
                "/api/evaluations/run", json={"session_id": "session_x"}
            )

        assert first.status_code == 200, first.text
        assert second.status_code == 429
        run_mock.assert_called_once()

    def test_a_different_session_is_not_blocked_by_another_ones_rate_limit(
        self, registre_vierge
    ):
        app = _build_app(user=_fake_user())
        client = TestClient(app, raise_server_exceptions=False)

        with (
            patch(
                "apowerb.routers.evaluations.resolve_session_context",
                new=AsyncMock(return_value=_ctx()),
            ),
            patch(
                "apowerb.routers.evaluations.run_and_persist",
                new=AsyncMock(return_value=[]),
            ) as run_mock,
        ):
            first = client.post(
                "/api/evaluations/run", json={"session_id": "session_a"}
            )
            second = client.post(
                "/api/evaluations/run", json={"session_id": "session_b"}
            )

        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert run_mock.call_count == 2

    def test_rate_limit_check_itself_raises_429_after_the_window_elapses(
        self, monkeypatch
    ):
        from apowerb.evaluation import run_service

        clock = {"t": 1000.0}
        monkeypatch.setattr(run_service.time, "monotonic", lambda: clock["t"])

        run_service.check_rerun_rate_limit(owner_id="me@example.com", session_id="s")
        with pytest.raises(HTTPException) as excinfo:
            run_service.check_rerun_rate_limit(owner_id="me@example.com", session_id="s")
        assert excinfo.value.status_code == 429

        clock["t"] += 16  # past evaluation_min_rerun_interval_seconds (15)
        run_service.check_rerun_rate_limit(owner_id="me@example.com", session_id="s")

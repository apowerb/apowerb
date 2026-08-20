"""One unreachable orchestrator, one answer -- on every route of this router.

`#89` taught `GET /pipelines` to answer 503 when the orchestrator cannot be
reached. The seven other routes of the same file were left as they were, and
the screen that calls several of them at once got a clean 503 on one call and
something else on each of the others.

"Something else" was measured before it was fixed, and it was not the 500 with
a stack trace one would expect. Under the default Mage orchestrator every
client method except `get_all_pipelines` swallowed the failure and returned
`[]` or `None`, so:

  * `GET .../schedules` answered `200 []` -- a dead orchestrator rendering as
    a healthy, empty dashboard, the exact failure `OrchestratorUnavailable`
    was introduced to end;
  * `.../schedules/{id}/runs` and `PUT .../schedules/{id}` answered **403**,
    "this schedule does not belong to your agents" -- the ownership check
    comparing the caller's agents against a list that came back empty because
    nobody answered, denying callers their own schedule in the name of a check
    that never ran;
  * `GET .../runs/{id}` answered **404**, "pipeline run not found", a claim
    about a run nobody had been able to look up.

Which is why this change does not stop at the router. Catching
`OrchestratorUnavailable` on seven routes that could never receive it would
have been green here and inert in production: the tests would have proved the
handler, not the behaviour. The clients now raise it wherever the orchestrator
cannot be reached at all, and `_initialize_pipeline` stops swallowing it.

The line held throughout: only a *transport* failure is an outage. An
orchestrator that answers 404 for a run that does not exist has answered, and
that answer is still a 404 -- `test_an_answered_404_is_not_an_outage` is what
keeps this change from replacing one lie with another.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest
import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apowerb.auth.dependencies import get_current_user
from apowerb.configs.settings import Settings
from apowerb.routers import scheduler as mod
from apowerb.scheduler import mage as mage_mod
from apowerb.scheduler.mage import MageAPIClient
from apowerb.scheduler.th2etl_client import (
    OrchestratorUnavailable,
    Th2etlAPIClient,
    orchestrator_is_unreachable,
)

AGENT_IDS = {"75", "agent75"}


def _settings(**overrides) -> Settings:
    """No orchestrator setting in this base -- see `test_orchestrator_absent`,
    where the same helper's silence is itself asserted."""
    base = dict(
        working_mode="development",
        encrypt_key="k" * 32,
        rag_webhook_secret="a-real-secret-value",
    )
    base.update(overrides)
    return Settings(**base)


# ---------------------------------------------------------------------------
# The routes
# ---------------------------------------------------------------------------

# Every route this router exposes. A route added without a line here is a route
# that goes back to answering 500, so the count is asserted below.
ROUTES = [
    ("GET", "/api/pipelines", None),
    ("POST", "/api/pipelines/agents/triggers", {"agent_id": "75", "agent_name": "a"}),
    ("GET", "/api/pipelines/agents/schedules", None),
    ("GET", "/api/pipelines/agents/schedules/1/runs", None),
    ("PUT", "/api/pipelines/runs/1/cancel", None),
    ("GET", "/api/pipelines/runs/1", None),
    ("GET", "/api/pipelines/runs/1/logs", None),
    ("PUT", "/api/pipelines/agents/schedules/1", {"status": "active"}),
]
IDS = [f"{m} {u}" for m, u, _ in ROUTES]


def test_the_table_covers_every_route_of_this_router():
    """A positive control on the table itself. Without it, a route added later
    is simply absent from every test below, and the suite stays green about a
    route it never calls."""
    assert len(ROUTES) == len(mod.router.routes)


class _DeadClient:
    """Unreachable, whichever method is called."""

    def __getattr__(self, name):
        def _boom(*args, **kwargs):
            raise OrchestratorUnavailable(
                "Mage is unreachable at http://localhost:6789: connection refused"
            )

        return _boom


class _LiveClient:
    """Answers every call this router makes, plausibly."""

    def get_all_pipelines(self):
        return [{"uuid": "agents"}]

    def get_pipeline_schedules(self, pipeline_uuid):
        return [{"id": "1", "name": "75", "status": "active"}]

    def get_pipeline_runs(self, schedule_id):
        return [{"id": 9, "status": "completed"}]

    def get_pipeline_run(self, run_id):
        return {"id": run_id, "scheduler_name": "75"}

    def get_pipeline_run_logs(self, run_id):
        return [{"id": 1, "run_id": run_id, "message": "step one"}]

    def cancel_pipeline_run(self, run_id):
        return {"id": run_id, "status": "cancelled"}

    def update_schedule(self, **kwargs):
        return {"id": kwargs["schedule_id"], "status": "active"}


def _no_db(_user_id):
    raise AssertionError(
        "the caller's agents were looked up in the database although the "
        "orchestrator was already known to be unreachable"
    )


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(mod.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: type(
        "U", (), {"email": "someone@example.test"}
    )()
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("method,url,body", ROUTES, ids=IDS)
def test_every_route_answers_503_when_the_orchestrator_is_unreachable(
    client, method, url, body
):
    with patch.object(mod, "scheduler_client", _DeadClient()), patch.object(
        mod, "process_agent_registration", _DeadClient().anything
    ), patch.object(mod, "get_settings", lambda: _settings()), patch.object(
        mod, "_get_user_agent_ids", _no_db
    ):
        got = client.request(method, url, json=body)
    assert got.status_code == 503, got.text


@pytest.mark.parametrize("method,url,body", ROUTES, ids=IDS)
def test_every_route_answers_normally_when_the_orchestrator_works(
    client, method, url, body
):
    """The positive control, on every route rather than one of them: answering
    503 unconditionally would satisfy the test above on all eight."""
    with patch.object(mod, "scheduler_client", _LiveClient()), patch.object(
        mod,
        "process_agent_registration",
        lambda **kwargs: {"schedule_id": 1, "trigger_token": "t", "status": "active"},
    ), patch.object(mod, "get_settings", lambda: _settings()), patch.object(
        mod, "_get_user_agent_ids", lambda _user_id: set(AGENT_IDS)
    ):
        got = client.request(method, url, json=body)
    assert got.status_code == 200, got.text


@pytest.mark.parametrize("method,url,body", ROUTES, ids=IDS)
def test_nothing_configured_says_how_to_turn_the_feature_off(client, method, url, body):
    """Same sentence on every route, not just on `/pipelines`: operators who
    never installed an orchestrator meet whichever route their screen calls
    first."""
    with patch.object(mod, "scheduler_client", _DeadClient()), patch.object(
        mod, "process_agent_registration", _DeadClient().anything
    ), patch.object(mod, "get_settings", lambda: _settings()), patch.object(
        mod, "_get_user_agent_ids", _no_db
    ):
        detail = client.request(method, url, json=body).json()["detail"]
    assert "SCHEDULER_ENABLED" in detail
    # He never typed this address; quoting it back at him is what made the
    # original message unreadable.
    assert "localhost:6789" not in detail


@pytest.mark.parametrize("method,url,body", ROUTES, ids=IDS)
def test_a_configured_orchestrator_that_is_down_stays_loud(
    client, caplog, method, url, body
):
    """The regression to avoid on all eight, not only on the one #89 covered:
    a real outage of an orchestrator somebody installed has to reach the logs
    at ERROR."""
    cfg = _settings(api_key="a-real-mage-key")
    with caplog.at_level(logging.ERROR), patch.object(
        mod, "scheduler_client", _DeadClient()
    ), patch.object(
        mod, "process_agent_registration", _DeadClient().anything
    ), patch.object(mod, "get_settings", lambda: cfg), patch.object(
        mod, "_get_user_agent_ids", _no_db
    ):
        got = client.request(method, url, json=body)
    assert got.status_code == 503
    assert [r for r in caplog.records if "unreachable" in r.getMessage()]


def test_an_unreachable_orchestrator_is_not_a_403(client):
    """The answer this route used to give, and the reason the fix could not
    stop at the router: no schedules came back, so the caller's own schedule
    matched nothing and he was told it was not his."""
    with patch.object(mod, "scheduler_client", _DeadClient()), patch.object(
        mod, "get_settings", lambda: _settings()
    ), patch.object(mod, "_get_user_agent_ids", _no_db):
        got = client.get("/api/pipelines/agents/schedules/1/runs")
    assert got.status_code == 503
    assert "belong to your agents" not in got.text


def test_an_unreachable_orchestrator_is_not_an_empty_list(client):
    """`200 []` is the answer that let an outage run three weeks unseen."""
    with patch.object(mod, "scheduler_client", _DeadClient()), patch.object(
        mod, "get_settings", lambda: _settings()
    ), patch.object(mod, "_get_user_agent_ids", _no_db):
        got = client.get("/api/pipelines/agents/schedules")
    assert got.status_code == 503
    assert got.json() != []


def test_an_unreachable_orchestrator_is_not_a_missing_run(client):
    """404 says the run does not exist. Nobody was able to look."""
    with patch.object(mod, "scheduler_client", _DeadClient()), patch.object(
        mod, "get_settings", lambda: _settings()
    ), patch.object(mod, "_get_user_agent_ids", _no_db):
        got = client.get("/api/pipelines/runs/1")
    assert got.status_code == 503
    assert "not found" not in got.text


def test_a_run_the_orchestrator_says_nothing_about_is_still_a_404(client):
    """The other half of the line above: the client returning `None` for a run
    the orchestrator answered about still means 404, and turning that into 503
    would move the lie instead of removing it."""

    class _AnswersNothing(_LiveClient):
        def get_pipeline_run(self, run_id):
            return None

    with patch.object(mod, "scheduler_client", _AnswersNothing()), patch.object(
        mod, "get_settings", lambda: _settings()
    ), patch.object(mod, "_get_user_agent_ids", lambda _u: set(AGENT_IDS)):
        got = client.get("/api/pipelines/runs/1")
    assert got.status_code == 404


# ---------------------------------------------------------------------------
# The discriminant: reached, or not reached at all
# ---------------------------------------------------------------------------


def test_a_refused_connection_is_unreachable():
    assert orchestrator_is_unreachable(requests.ConnectionError("refused")) is True


def test_a_timeout_is_unreachable():
    assert orchestrator_is_unreachable(requests.Timeout("timed out")) is True


def test_an_answered_404_is_not_an_outage():
    """The negative control. `raise_for_status()` attaches the response, and a
    response means the orchestrator was there and said something true."""
    resp = requests.Response()
    resp.status_code = 404
    assert orchestrator_is_unreachable(
        requests.HTTPError("404 Not Found", response=resp)
    ) is False


def test_something_that_is_not_a_request_failure_is_not_an_outage():
    assert orchestrator_is_unreachable(ValueError("bad json")) is False


# ---------------------------------------------------------------------------
# The clients: the router can only catch what reaches it
# ---------------------------------------------------------------------------


def _dead_mage() -> MageAPIClient:
    c = MageAPIClient.__new__(MageAPIClient)
    c.base_url, c.api_key, c.oauth_token, c.project_name = (
        "http://mage.internal:6789",
        "k",
        None,
        "p",
    )
    c._initialized = True
    return c


MAGE_CALLS = [
    ("get_all_pipelines", lambda c: c.get_all_pipelines()),
    ("get_pipeline_schedules", lambda c: c.get_pipeline_schedules("agents")),
    ("get_pipeline_runs", lambda c: c.get_pipeline_runs(1)),
    ("get_pipeline_run", lambda c: c.get_pipeline_run(1)),
    ("cancel_pipeline_run", lambda c: c.cancel_pipeline_run(1)),
]


@pytest.mark.parametrize("name,call", MAGE_CALLS, ids=[n for n, _ in MAGE_CALLS])
def test_mage_raises_rather_than_swallowing_when_unreachable(name, call):
    """Each of these returned `[]` or `None` on a refused connection, which is
    what made the router's handler unreachable in production."""
    boom = requests.ConnectionError("refused")
    with patch.object(mage_mod.requests, "get", side_effect=boom), patch.object(
        mage_mod.requests, "put", side_effect=boom
    ):
        with pytest.raises(OrchestratorUnavailable):
            call(_dead_mage())


@pytest.mark.parametrize("name,call", MAGE_CALLS, ids=[n for n, _ in MAGE_CALLS])
def test_mage_keeps_its_fallback_when_the_orchestrator_answered(name, call):
    """The negative control on the clients: an HTTP error response is an
    answer, and the old degraded return is still the right one for it. Without
    this, raising on everything would pass the test above."""
    resp = requests.Response()
    resp.status_code = 500
    boom = requests.HTTPError("500", response=resp)
    with patch.object(mage_mod.requests, "get", side_effect=boom), patch.object(
        mage_mod.requests, "put", side_effect=boom
    ):
        if name == "get_all_pipelines":
            # Unchanged by this commit: it has raised on any failure since the
            # outage that introduced it, and callers depend on that.
            with pytest.raises(OrchestratorUnavailable):
                call(_dead_mage())
        else:
            assert call(_dead_mage()) in ([], None)


def test_mage_run_logs_stay_a_no_op():
    """Mage has no per-run log endpoint, so `[]` here is a fact about Mage, not
    a swallowed outage -- it must not start answering 503."""
    with patch.object(mage_mod.requests, "get", side_effect=AssertionError("no call")):
        assert _dead_mage().get_pipeline_run_logs(1) == []


TH2ETL_CALLS = [
    ("get_all_pipelines", lambda c: c.get_all_pipelines()),
    ("get_pipeline_schedules", lambda c: c.get_pipeline_schedules("agents")),
    ("get_pipeline_runs", lambda c: c.get_pipeline_runs("75")),
    ("get_pipeline_run", lambda c: c.get_pipeline_run(1)),
    ("get_pipeline_run_logs", lambda c: c.get_pipeline_run_logs(1)),
    ("cancel_pipeline_run", lambda c: c.cancel_pipeline_run(1)),
]


@pytest.mark.parametrize("name,call", TH2ETL_CALLS, ids=[n for n, _ in TH2ETL_CALLS])
def test_th2etl_raises_rather_than_leaking_a_connection_error(name, call):
    """Three of these let `requests.ConnectionError` out raw, which reached the
    API as a 500 with a stack trace."""
    c = Th2etlAPIClient("http://etl.internal:8009", timeout=1, api_key="k")
    boom = requests.ConnectionError("refused")
    with patch.object(c._http, "get", side_effect=boom), patch.object(
        c._http, "post", side_effect=boom
    ):
        with pytest.raises(OrchestratorUnavailable):
            call(c)


def test_initialize_pipeline_stops_swallowing_the_outage():
    """It turned the outage into `Exception("Failed to initialize pipeline
    infrastructure")`, which reached the API as a 500 about the pipeline. The
    docstring of `get_all_pipelines` had already noted that this caller "was
    written around an exception it never got"; it was still catching it."""
    orchestrator = mage_mod.AgentOrchestrator(client=_dead_mage())
    boom = requests.ConnectionError("refused")
    with patch.object(mage_mod.requests, "get", side_effect=boom):
        with pytest.raises(OrchestratorUnavailable):
            orchestrator._initialize_pipeline()


def test_th2etl_keeps_its_fallback_when_the_orchestrator_answered():
    """The negative control on th2etl, and the mistake this change made once.

    An upstream 500 is an answer, and the degraded `[]` is still the right
    reply to it. The first discriminant asked "is this a request failure
    carrying no response", which is true of a refused connection -- and also
    true under the fake in `test_th2etl_client`, which collapses `HTTPError`
    and `RequestException` to bare `Exception`. An answered 500 was therefore
    read as an outage: a 503 over an orchestrator that was up and saying so.
    """
    resp = requests.Response()
    resp.status_code = 500
    boom = requests.HTTPError("500", response=resp)
    c = Th2etlAPIClient("http://etl.internal:8009", timeout=1, api_key="k")
    with patch.object(c._http, "get", side_effect=boom):
        assert c.get_pipeline_run_logs(1) == []
        assert c.get_pipeline_schedules("agents") == []
        assert c.get_pipeline_runs("75") == []

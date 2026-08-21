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


# A list and a lookup are different questions, and only one of them has a
# legitimate negative answer.
MAGE_LISTS = MAGE_CALLS[:3]
MAGE_LOOKUPS = MAGE_CALLS[3:]


@pytest.mark.parametrize("name,call", MAGE_LISTS, ids=[n for n, _ in MAGE_LISTS])
def test_a_mage_list_never_degrades_to_an_empty_one(name, call):
    """An empty list is an answer -- "there are none" -- and a rejected key is
    not that answer.

    A screen showing no schedules because the API key expired is the same lie
    as one showing none because the orchestrator is dead; the caller cannot
    tell them apart, and neither is true. `get_all_pipelines` has refused to
    degrade since the outage that introduced it, and the two lists feeding the
    same screen now hold the same line.
    """
    resp = requests.Response()
    resp.status_code = 401
    boom = requests.HTTPError("401", response=resp)
    with patch.object(mage_mod.requests, "get", side_effect=boom), patch.object(
        mage_mod.requests, "put", side_effect=boom
    ):
        with pytest.raises(OrchestratorUnavailable):
            call(_dead_mage())


@pytest.mark.parametrize("name,call", MAGE_LOOKUPS, ids=[n for n, _ in MAGE_LOOKUPS])
def test_a_mage_lookup_the_orchestrator_answered_still_returns_none(name, call):
    """The other side of that line, and why it cannot simply be "raise on
    everything": asking after one run is a question with a legitimate negative
    answer, and `None` is what the route turns into a 404. Without this test,
    raising everywhere would satisfy the one above."""
    resp = requests.Response()
    resp.status_code = 404
    boom = requests.HTTPError("404", response=resp)
    with patch.object(mage_mod.requests, "get", side_effect=boom), patch.object(
        mage_mod.requests, "put", side_effect=boom
    ):
        assert call(_dead_mage()) is None


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
        # An answered 500 is not an outage -- but it is not an empty list
        # either. The message says which of the two happened; the status code
        # the caller sees is 503 in both cases, because in both cases we cannot
        # tell them what they asked for.
        for call in (
            lambda: c.get_pipeline_run_logs(1),
            lambda: c.get_pipeline_schedules("agents"),
            lambda: c.get_pipeline_runs("75"),
        ):
            with pytest.raises(OrchestratorUnavailable) as raised:
                call()
            assert "answered" in str(raised.value)


# ---------------------------------------------------------------------------
# Second pass: what an adversarial review found the first one had left open
# ---------------------------------------------------------------------------


def _answered(status: int) -> requests.HTTPError:
    """An orchestrator that replied, with that status."""
    resp = requests.Response()
    resp.status_code = status
    return requests.HTTPError(f"{status}", response=resp)


@pytest.mark.parametrize("status", [502, 503, 504])
def test_a_gateway_that_could_not_reach_it_is_an_outage(status):
    """The hole the first pass left open, and the one that mattered most.

    Behind a reverse proxy -- nginx, Traefik, an ALB, which is the ordinary
    deployment and not an edge case -- an orchestrator that is down does not
    refuse the connection. The proxy answers 502. Reading that as "it replied,
    degrade quietly" put `200 []`, the spurious 403 and the spurious 404 back
    on every route, in the exact deployment shape most likely to be running.
    """
    assert orchestrator_is_unreachable(_answered(status)) is True


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 500])
def test_an_answered_error_that_is_not_a_gateway_is_not_an_outage(status):
    """The other half of that line, and the reason it cannot simply be "any
    error status". A 401 is a fact about the key, a 404 about the run, a 500
    about the orchestrator's own code. None of them mean nobody was there, and
    answering "unreachable" sends whoever reads it to check the network."""
    assert orchestrator_is_unreachable(_answered(status)) is False


@pytest.mark.parametrize("status", [502, 504])
@pytest.mark.parametrize("name,call", MAGE_CALLS, ids=[n for n, _ in MAGE_CALLS])
def test_mage_reports_a_gateway_outage(name, call, status):
    boom = _answered(status)
    with patch.object(mage_mod.requests, "get", side_effect=boom), patch.object(
        mage_mod.requests, "put", side_effect=boom
    ):
        with pytest.raises(OrchestratorUnavailable):
            call(_dead_mage())


@pytest.mark.parametrize("status", [502, 504])
@pytest.mark.parametrize("name,call", TH2ETL_CALLS, ids=[n for n, _ in TH2ETL_CALLS])
def test_th2etl_reports_a_gateway_outage(name, call, status):
    c = Th2etlAPIClient("http://etl.internal:8009", timeout=1, api_key="k")
    boom = _answered(status)
    with patch.object(c._http, "get", side_effect=boom), patch.object(
        c._http, "post", side_effect=boom
    ):
        with pytest.raises(OrchestratorUnavailable):
            call(c)


@pytest.mark.parametrize(
    "name,call",
    [
        ("get_pipeline_run", lambda c: c.get_pipeline_run(1)),
        ("cancel_pipeline_run", lambda c: c.cancel_pipeline_run(1)),
    ],
    ids=["get_pipeline_run", "cancel_pipeline_run"],
)
def test_th2etl_answers_none_for_a_run_the_orchestrator_answered_about(name, call):
    """These two re-raised the raw `HTTPError`, so a run that simply does not
    exist reached the API as a 500 with a trace -- where the same lookup under
    Mage has always answered 404. The th2etl negative control never called
    them, which is how the asymmetry survived the first pass."""
    boom = _answered(404)
    c = Th2etlAPIClient("http://etl.internal:8009", timeout=1, api_key="k")
    with patch.object(c._http, "get", side_effect=boom), patch.object(
        c._http, "post", side_effect=boom
    ):
        assert call(c) is None


class _Ok:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_an_orchestrator_that_dies_after_the_connectivity_check_still_answers_503(client):
    """`_initialize_pipeline` guarded its step 0 and only its step 0.

    A Mage that answers the connectivity check and falls over a moment later
    went through `pipeline_exists` -- which swallowed the failure into `False`
    -- and came back out as `Exception("Failed to initialize pipeline
    infrastructure")`, i.e. a bare 500 on `POST /pipelines/agents/triggers`.
    The very failure this whole change removes, on one of the routes it claims
    to have fixed, reachable by nothing more exotic than bad timing.
    """
    alive_then_dead = [
        _Ok({"pipelines": []}),
        requests.ConnectionError("refused"),
        requests.ConnectionError("refused"),
    ]
    orchestrator = mage_mod.AgentOrchestrator(client=_dead_mage())
    with patch.object(mage_mod.requests, "get", side_effect=alive_then_dead), patch.object(
        mage_mod, "_orchestrator_instance", orchestrator
    ), patch.object(mod, "get_settings", lambda: _settings()), patch.object(
        mod, "_get_user_agent_ids", _no_db
    ):
        got = client.post(
            "/api/pipelines/agents/triggers",
            json={"agent_id": "75", "agent_name": "a"},
        )
    assert got.status_code == 503, got.text


# ---------------------------------------------------------------------------
# The sister router: same client, same outage, same answer
# ---------------------------------------------------------------------------


@pytest.fixture
def dashboard_client():
    from unittest.mock import AsyncMock

    from apowerb.bi.dependencies import get_dashboard_service
    from apowerb.bi.refresh_router import router as refresh_router

    app = FastAPI()
    app.include_router(refresh_router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: type(
        "U", (), {"email": "someone@example.test"}
    )()
    app.dependency_overrides[get_dashboard_service] = lambda: AsyncMock()
    return TestClient(app, raise_server_exceptions=False)


def _dead_orchestrator():
    from types import SimpleNamespace

    return SimpleNamespace(PIPELINE_UUID="agents", client=_DeadClient())


def test_the_dashboard_schedule_list_answers_503_not_500(dashboard_client):
    """The dashboard-refresh router calls the same client, so it meets the same
    outage. Before the clients learned to report one it answered `200 []` here
    -- the healthy-looking empty dashboard, one more time. Letting it fall into
    its own generic 500 instead would have traded one wrong answer for another,
    and would answer 500 on every installation that simply never set an
    orchestrator up."""
    import apowerb.bi.refresh_router as bi

    with patch.object(bi, "get_orchestrator", _dead_orchestrator), patch.object(
        bi, "get_settings", lambda: _settings()
    ), patch.object(bi, "fetch_agents", lambda _email: []):
        got = dashboard_client.get("/api/v1/dashboards/d1/schedules")
    assert got.status_code == 503, got.text
    assert "SCHEDULER_ENABLED" in got.json()["detail"]


def test_deleting_a_dashboard_schedule_answers_503_not_404(dashboard_client):
    """Its `except Exception: all_schedules = []` absorbed the outage exactly
    as it used to absorb the empty return, and the caller was told their
    schedule did not exist. The same shape this change removes from the
    scheduler router, eighty lines away in a sister file."""
    import apowerb.bi.refresh_router as bi

    with patch.object(bi, "get_orchestrator", _dead_orchestrator), patch.object(
        bi, "get_settings", lambda: _settings()
    ), patch.object(bi, "fetch_agents", lambda _email: []):
        got = dashboard_client.delete("/api/v1/dashboards/d1/schedules/1")
    assert got.status_code == 503, got.text
    assert "not found" not in got.text.lower()


# ---------------------------------------------------------------------------
# Third pass: a handler is only as good as what reaches it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        requests.exceptions.ChunkedEncodingError("the body stopped mid-flight"),
        requests.exceptions.ContentDecodingError("a broken content encoding"),
    ],
    ids=["body cut", "broken encoding"],
)
def test_any_failure_that_never_got_an_answer_is_an_outage(exc):
    """Naming `ConnectionError` and `Timeout` explicitly was the same bug in a
    narrower disguise. Neither of these is either, both mean no answer came
    back, and each one put the quiet `[]` back on a route."""
    assert orchestrator_is_unreachable(exc) is True


def test_a_redirect_loop_is_an_outage():
    """Built the way `requests` actually raises it.

    `sessions.py` attaches the last redirect to `TooManyRedirects`, so asking
    "did a response come back" answers yes and the generic rule misses it. An
    earlier version of this test constructed the exception by hand *without*
    that response and passed -- green about an input that cannot occur, while
    the real one went on being read as "it answered, degrade quietly". A test
    that cannot fail is worse than no test: it is a claim of coverage.
    """
    last_redirect = requests.Response()
    last_redirect.status_code = 302
    assert orchestrator_is_unreachable(
        requests.exceptions.TooManyRedirects(
            "Exceeded 30 redirects.", response=last_redirect
        )
    ) is True


def test_a_body_that_came_back_and_would_not_parse_is_not_an_outage():
    """The one failure that carries no response and is still an answer: the
    orchestrator replied, we could not read it. A fact about the payload, not
    about reachability -- and the reason the rule cannot simply be "no
    response means no answer"."""
    assert orchestrator_is_unreachable(
        requests.exceptions.InvalidJSONError("not json")
    ) is False


MAGE_SETUP_CALLS = [
    ("pipeline_exists", lambda c: c.pipeline_exists("agents")),
    ("create_pipeline", lambda c: c.create_pipeline("agents")),
    ("block_exists", lambda c: c.block_exists("agents", "agent_exe")),
    ("create_block", lambda c: c.create_block("agents", "agent_exe", "print()")),
]


@pytest.mark.parametrize("status", [502, 503, 504])
@pytest.mark.parametrize(
    "name,call", MAGE_SETUP_CALLS, ids=[n for n, _ in MAGE_SETUP_CALLS]
)
def test_the_setup_calls_report_a_dead_gateway(name, call, status):
    """These four read `status_code` by hand and never call
    `raise_for_status()`, so a 502 raised nothing at all -- and the check the
    previous commit added to their `except` was decoration. Behind a proxy,
    `POST /pipelines/agents/triggers` went on answering a bare 500.
    """
    gateway = _Ok({}, status=status)
    with patch.object(mage_mod.requests, "get", return_value=gateway), patch.object(
        mage_mod.requests, "post", return_value=gateway
    ):
        with pytest.raises(OrchestratorUnavailable):
            call(_dead_mage())


@pytest.mark.parametrize(
    "name,call", MAGE_SETUP_CALLS, ids=[n for n, _ in MAGE_SETUP_CALLS]
)
def test_the_setup_calls_report_a_refused_connection(name, call):
    boom = requests.ConnectionError("refused")
    with patch.object(mage_mod.requests, "get", side_effect=boom), patch.object(
        mage_mod.requests, "post", side_effect=boom
    ):
        with pytest.raises(OrchestratorUnavailable):
            call(_dead_mage())


def test_th2etl_pipeline_exists_reports_an_outage():
    """It answered `False` for every failure mode there is, so the th2etl half
    of `POST /pipelines/agents/triggers` kept its bare 500 while the Mage half
    was declared fixed."""
    c = Th2etlAPIClient("http://etl.internal:8009", timeout=1, api_key="k")
    with patch.object(c._http, "get", side_effect=requests.ConnectionError("refused")):
        with pytest.raises(OrchestratorUnavailable):
            c.pipeline_exists("agents")


def test_an_orchestrator_that_dies_at_the_block_check_still_answers_503(client):
    """The twin of the pipeline-check test above, and the mutant that survived:
    reverting the second of the two symmetric guards left all tests green. Two
    guards written together need two tests written together."""
    alive_then_dead = [
        _Ok({"pipelines": []}),  # step 0: connectivity
        _Ok({"pipeline": {"uuid": "agents"}}),  # step 1: the pipeline is there
        requests.ConnectionError("refused"),  # step 2: the block check
        requests.ConnectionError("refused"),
    ]
    orchestrator = mage_mod.AgentOrchestrator(client=_dead_mage())
    with patch.object(mage_mod.requests, "get", side_effect=alive_then_dead), patch.object(
        mage_mod.requests, "post", side_effect=requests.ConnectionError("refused")
    ), patch.object(mage_mod, "_orchestrator_instance", orchestrator), patch.object(
        mod, "get_settings", lambda: _settings()
    ), patch.object(mod, "_get_user_agent_ids", _no_db):
        got = client.post(
            "/api/pipelines/agents/triggers",
            json={"agent_id": "75", "agent_name": "a"},
        )
    assert got.status_code == 503, got.text


def test_a_rejected_key_does_not_render_as_an_empty_schedule_list(client):
    """The route-level proof of the decision, and the gap that let it survive
    two passes: every check on this behaviour lived at the client layer, so
    nothing ever asserted the status code the caller actually receives. A 401
    used to arrive as `200 []` -- an orchestrator saying "no" rendered as a
    tidy, empty, healthy screen."""
    resp = requests.Response()
    resp.status_code = 401
    boom = requests.HTTPError("401", response=resp)
    with patch.object(mage_mod.requests, "get", side_effect=boom), patch.object(
        mod, "scheduler_client", _dead_mage()
    ), patch.object(mod, "get_settings", lambda: _settings()), patch.object(
        mod, "_get_user_agent_ids", _no_db
    ):
        got = client.get("/api/pipelines/agents/schedules")
    assert got.status_code == 503, got.text
    assert got.json() != []


@pytest.mark.parametrize(
    "name,call",
    [
        ("get_pipeline_run", lambda c: c.get_pipeline_run(1)),
        ("cancel_pipeline_run", lambda c: c.cancel_pipeline_run(1)),
    ],
    ids=["get_pipeline_run", "cancel_pipeline_run"],
)
def test_th2etl_survives_a_body_it_cannot_read(name, call):
    """`resp.json()` sat outside the guarded block in both, so a 200 carrying a
    body that would not parse left as a raw decoding error and reached the API
    as a bare 500 -- where the Mage twin has always degraded to `None`."""

    class _Unreadable:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            raise ValueError("not json")

    c = Th2etlAPIClient("http://etl.internal:8009", timeout=1, api_key="k")
    with patch.object(c._http, "get", return_value=_Unreadable()), patch.object(
        c._http, "post", return_value=_Unreadable()
    ):
        assert call(c) is None


# ---------------------------------------------------------------------------
# The restructuring: one place where a failed call is classified
# ---------------------------------------------------------------------------


class _Answered:
    """A 200 whose body is not what the caller was promised."""

    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


@pytest.mark.parametrize(
    "payload",
    [{"error": "not found"}, {"status": "failed"}, {}],
    ids=["error envelope", "some other envelope", "empty object"],
)
@pytest.mark.parametrize("name,call", MAGE_LISTS, ids=[n for n, _ in MAGE_LISTS])
def test_a_body_without_the_list_it_promised_is_not_an_empty_list(name, call, payload):
    """The failure no `except` could ever have caught, and the one that
    survived three rounds of review.

    The body arrived, whole and parseable. `.get("pipeline_schedules", [])`
    handed back an empty list, and Mage answering `{"error": "not found"}`
    rendered as a screen saying "you have none" -- with nothing raised for
    anyone to catch, no log line, and no way for the caller to tell. It reached
    `get_all_pipelines` too, the one method considered safe since the outage
    that started all of this.
    """
    with patch.object(
        mage_mod.requests, "get", return_value=_Answered(payload)
    ), patch.object(mage_mod.requests, "put", return_value=_Answered(payload)):
        with pytest.raises(OrchestratorUnavailable):
            call(_dead_mage())


def test_an_error_envelope_does_not_render_as_an_empty_screen(client):
    """The same thing said at the route, where it is the caller's problem."""
    with patch.object(
        mage_mod.requests, "get", return_value=_Answered({"error": "not found"})
    ), patch.object(mod, "scheduler_client", _dead_mage()), patch.object(
        mod, "get_settings", lambda: _settings()
    ), patch.object(mod, "_get_user_agent_ids", _no_db):
        got = client.get("/api/pipelines/agents/schedules")
    assert got.status_code == 503, got.text
    assert got.json() != []


def test_a_scheduler_carrying_no_name_is_not_an_empty_list():
    """th2etl's mapping loop sat outside every guard: `s["name"]` on an item
    without one raised a bare `KeyError`, and four routes answered a 500 with
    no body at all."""
    c = Th2etlAPIClient("http://etl.internal:8009", timeout=1, api_key="k")
    with patch.object(
        c._http,
        "get",
        return_value=_Answered([{"pipeline_name": "agents", "active": True}]),
    ):
        with pytest.raises(OrchestratorUnavailable):
            c.get_pipeline_schedules("agents")


def test_a_scheduler_listing_that_is_not_a_listing_is_not_an_empty_one():
    """Iterating a dict walked its keys, matched nothing and returned `[]`: an
    answer we never received, in the shape of one we did."""
    c = Th2etlAPIClient("http://etl.internal:8009", timeout=1, api_key="k")
    with patch.object(c._http, "get", return_value=_Answered({"detail": "nope"})):
        with pytest.raises(OrchestratorUnavailable):
            c.get_pipeline_schedules("agents")


def test_an_unreachable_orchestrator_is_told_apart_from_one_that_answered():
    """The distinction the whole restructuring turns on. Both leave as 503 --
    in both cases the caller cannot be told what it asked -- but only one of
    them means somebody should go and look at the network."""
    c = Th2etlAPIClient("http://etl.internal:8009", timeout=1, api_key="k")
    with patch.object(c._http, "get", side_effect=requests.ConnectionError("refused")):
        with pytest.raises(OrchestratorUnavailable) as gone:
            c.get_all_pipelines()
    assert gone.value.unreachable is True

    with patch.object(c._http, "get", return_value=_Answered({"error": "nope"})):
        with pytest.raises(OrchestratorUnavailable) as answered:
            c.get_all_pipelines()
    assert answered.value.unreachable is False


def test_updating_a_schedule_no_longer_leaks_the_outage(client):
    """The race the previous commit documented and left open: the ownership
    check passes, the orchestrator dies, and the update itself answered a bare
    500 because nothing in this route wrapped it."""

    class _DiesOnTheUpdate(_LiveClient):
        def update_schedule(self, **kwargs):
            raise OrchestratorUnavailable("Mage is unreachable at http://x: boom")

    with patch.object(mod, "scheduler_client", _DiesOnTheUpdate()), patch.object(
        mod, "get_settings", lambda: _settings()
    ), patch.object(mod, "_get_user_agent_ids", lambda _u: set(AGENT_IDS)):
        got = client.put(
            "/api/pipelines/agents/schedules/1", json={"status": "active"}
        )
    assert got.status_code == 503, got.text


def test_scheduling_a_chart_refresh_answers_503(client):
    """A route nobody had enumerated across three passes, reached through
    `schedule_agent_run` and sharing the same client as the scheduler
    screens."""
    from unittest.mock import AsyncMock

    import apowerb.bi.chart_refresh_router as chart_mod
    from apowerb.bi.dependencies import get_chart_service

    app = FastAPI()
    app.include_router(chart_mod.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: type(
        "U", (), {"email": "someone@example.test"}
    )()
    app.dependency_overrides[get_chart_service] = lambda: AsyncMock()
    chart_client = TestClient(app, raise_server_exceptions=False)

    async def _dead(**kwargs):
        raise OrchestratorUnavailable("Mage is unreachable at http://x: boom")

    with patch.object(chart_mod, "schedule_agent_run", _dead), patch.object(
        chart_mod, "get_agent_by_id", lambda *a, **k: {"agent_id": "75"}
    ), patch.object(chart_mod, "get_settings", lambda: _settings()):
        got = chart_client.post(
            "/api/v1/charts/c1/schedule-refresh",
            json={"agent_id": 75, "interval": "@hourly"},
        )
    assert got.status_code == 503, got.text


def test_no_orchestrator_call_bypasses_the_single_classification_point():
    """The guard against a seventh generation of this bug.

    Three rounds of review each found the same failure in a method that had
    invented its own silence -- and a method written the old way tomorrow would
    do it again, invisibly: it would return `[]`, and every behavioural test
    here would stay green because none of them would call it. So this one reads
    the source.

    It reads it as a syntax tree, not as text. The first version matched a
    regular expression within 340 characters of a `self._ask(`, and a reviewer
    broke it twice in a minute: once by putting an unclassified call *near* a
    classified one, once by using `.patch(`, a verb the pattern did not list.
    Both re-introduced the exact bug, both left the test green. Proximity is not
    containment, and an alternation is not a category.

    The Mage *block content* -- Python this file hands to Mage, executed inside
    Mage -- is a string literal, so it is not in the tree and needs no
    exception carved out for it.
    """
    import ast
    import pathlib

    from apowerb.scheduler import mage as mage_src
    from apowerb.scheduler import th2etl_client as etl_src

    VERBS = {"get", "post", "put", "delete", "patch", "head", "options", "request"}

    def _http_calls(node):
        """Line numbers of every HTTP call made on `requests` or `self._http`."""
        out = set()
        for n in ast.walk(node):
            if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)):
                continue
            if n.func.attr not in VERBS:
                continue
            base = n.func.value
            on_requests = isinstance(base, ast.Name) and base.id == "requests"
            on_session = isinstance(base, ast.Attribute) and base.attr == "_http"
            if on_requests or on_session:
                out.add(n.lineno)
        return out

    def _bypasses(module) -> set[int]:
        tree = ast.parse(pathlib.Path(module.__file__).read_text())
        classified = set()
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            goes_through = (
                isinstance(n.func, ast.Attribute) and n.func.attr == "_ask"
            ) or (isinstance(n.func, ast.Name) and n.func.id == "ask_orchestrator")
            if goes_through:
                classified |= _http_calls(n)
        return _http_calls(tree) - classified

    assert _bypasses(etl_src) == set()
    assert _bypasses(mage_src) == set()

def test_a_null_error_field_alongside_a_good_payload_is_not_an_outage():
    """The key is not the answer; the value is.

    Reading `"error" in body` would have called `{"pipelines": [...], "error":
    null}` a failure -- a working orchestrator turned into a 503 by a field it
    politely set to nothing.
    """
    with patch.object(
        mage_mod.requests,
        "get",
        return_value=_Answered({"pipelines": [{"uuid": "agents"}], "error": None}),
    ):
        assert _dead_mage().get_all_pipelines() == [{"uuid": "agents"}]


def test_a_populated_error_field_is_an_outage():
    """The negative control on the line above: without it, ignoring the
    envelope entirely would pass."""
    with patch.object(
        mage_mod.requests,
        "get",
        return_value=_Answered({"pipelines": [], "error": "not found"}),
    ):
        with pytest.raises(OrchestratorUnavailable):
            _dead_mage().get_all_pipelines()


MAGE_WRITES_WITHOUT_A_KEY = [
    ("create_pipeline", lambda c: c.create_pipeline("agents")),
    ("create_block", lambda c: c.create_block("agents", "agent_exe", "print()")),
    ("trigger_pipeline", lambda c: c.trigger_pipeline(1, "tok")),
    (
        "trigger_pipeline_run_for_schedule",
        lambda c: c.trigger_pipeline_run_for_schedule(1),
    ),
]


@pytest.mark.parametrize(
    "name,call",
    MAGE_WRITES_WITHOUT_A_KEY,
    ids=[n for n, _ in MAGE_WRITES_WITHOUT_A_KEY],
)
def test_mage_saying_no_is_not_a_success(name, call):
    """Mage wraps its refusals, and these four calls unwrap no key.

    Gating the envelope check on `expect` was the next version of the same
    mistake: `{"error": "duplicate pipeline uuid"}` came back as a truthy dict,
    `if result:` read it as created, and the log said "Pipeline created
    successfully!" over a pipeline Mage had just refused to create. Which half
    of the API you are talking to is a property of the API, not of whether this
    particular call happens to name a key.
    """
    with patch.object(
        mage_mod.requests, "post", return_value=_Answered({"error": "no"})
    ), patch.object(mage_mod.requests, "put", return_value=_Answered({"error": "no"})):
        assert call(_dead_mage()) is None


def test_a_th2etl_run_that_failed_is_still_a_run():
    """A th2etl run carries the exception that killed it, by design. Reading
    that as the orchestrator refusing made `get_pipeline_run` answer `None`,
    and the route turned it into "pipeline run not found" -- a fresh lie about
    a run, introduced by the change that set out to stop lies about runs."""
    failed = {
        "id": 42,
        "scheduler_name": "75",
        "status": "failed",
        "error": "ValueError: the agent blew up",
    }
    c = Th2etlAPIClient("http://etl.internal:8009", timeout=1, api_key="k")
    with patch.object(c._http, "get", return_value=_Answered(failed)):
        run = c.get_pipeline_run(42)
    assert run is not None
    assert run["error"] == "ValueError: the agent blew up"


@pytest.mark.parametrize(
    "name,call",
    [
        ("get_pipeline_run", lambda c: c.get_pipeline_run(1)),
        ("cancel_pipeline_run", lambda c: c.cancel_pipeline_run(1)),
    ],
    ids=["get_pipeline_run", "cancel_pipeline_run"],
)
def test_a_run_that_came_back_as_a_list_is_not_a_run(name, call):
    """The mirror of the listing check. `_to_mage_run` passes non-dicts
    straight through, so an array arrived at the route as a run, and the
    `result is None` test that guards the 404 never fired."""
    c = Th2etlAPIClient("http://etl.internal:8009", timeout=1, api_key="k")
    with patch.object(c._http, "get", return_value=_Answered([1, 2, 3])), patch.object(
        c._http, "post", return_value=_Answered([1, 2, 3])
    ):
        with pytest.raises(OrchestratorUnavailable):
            call(c)


def _runner_client():
    import apowerb.routers.adk_runner as adk

    app = FastAPI()
    app.include_router(adk.router, prefix="/api/adk")
    app.dependency_overrides[get_current_user] = lambda: type(
        "U", (), {"email": "someone@example.test"}
    )()
    return adk, TestClient(app, raise_server_exceptions=False)


def _boom(*args, **kwargs):
    raise OrchestratorUnavailable("Mage is unreachable at http://x: boom")


async def _boom_async(*args, **kwargs):
    raise OrchestratorUnavailable("Mage is unreachable at http://x: boom")


def test_schedule_run_answers_503():
    """Wired to the translator in an earlier pass and left without a test --
    which a reviewer noticed before anyone else did.

    It shares the orchestrator client with the scheduler screens, and when it
    could not reach one it answered "check the agent_id and schedule_interval":
    whoever read that went looking at their own input for a fault that was
    never theirs.
    """
    adk, client = _runner_client()
    with patch.object(adk, "schedule_agent_run", _boom_async), patch.object(
        adk, "get_settings", lambda: _settings()
    ):
        got = client.post(
            "/api/adk/schedule_run",
            json={
                "agent_id": "75",
                "user_id": "someone@example.test",
                "schedule_interval": "@hourly",
                "session_id": "s1",
                "new_message": {"role": "user", "parts": [{"text": "go"}]},
            },
        )
    assert got.status_code == 503, got.text


def test_run_now_answers_503():
    """Its sibling, forty lines below it in the same file."""
    from apowerb.scheduler import run_agent_background as background

    adk, client = _runner_client()
    with patch.object(background, "get_agent_by_id", _boom), patch.object(
        adk, "get_settings", lambda: _settings()
    ):
        got = client.post(
            "/api/adk/run_now",
            json={"agent_id": "75", "user_id": "someone@example.test"},
        )
    assert got.status_code == 503, got.text


def test_scheduling_a_dashboard_refresh_answers_503(dashboard_client):
    """The third route of `refresh_router`, and the one that actually creates
    the schedule.

    Its two siblings had tests from the start; this one had the handler and no
    test, so removing the handler left the whole suite green -- a mutant a
    reviewer fired and watched survive. The file is named after a promise
    ("every scheduler route answers 503"), which makes an untested route worse
    than an unfixed one: it reads as covered.
    """
    import apowerb.bi.refresh_router as bi

    async def _dead(**kwargs):
        raise OrchestratorUnavailable("Mage is unreachable at http://x: boom")

    with patch.object(bi, "schedule_agent_run", _dead), patch.object(
        bi, "get_agent_by_id", lambda *a, **k: {"agent_id": "75", "agent_name": "a"}
    ), patch.object(bi, "get_settings", lambda: _settings()), patch.object(
        bi, "fetch_agents", lambda _email: []
    ):
        got = dashboard_client.post(
            "/api/v1/dashboards/d1/schedule-refresh",
            json={"agent_id": 75, "interval": "@hourly"},
        )
    assert got.status_code == 503, got.text

"""An orchestrator that was never installed is not an orchestrator that is down.

A deployment carrying no Mage logged `ERROR ... Mage is unreachable at
http://localhost:6789` on every visit to the Orchestrator screen -- 31 of them
over two days in one export. Nothing was broken: nobody had installed Mage
there, and `base_url` ships pointing at localhost, so the client dutifully
called the visitor's own machine and reported the failure at ERROR level.

An ERROR that means "you did not install an optional component" is worse than
useless: it teaches whoever reads the logs that ERROR lines are normal, which
is exactly how a real one gets scrolled past.

The 503 stays -- the screen must be able to say "no schedules are reachable"
rather than "you have no schedules", and that reasoning is written above the
handler. What changes is the level, and the sentence: an operator who never
wanted a scheduler is told how to turn it off, not that a machine he never
configured is unreachable.

The discriminant is the same one the public-URL guard uses: a setting still
holding its shipped default was never provided. `model_fields_set` says so.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apowerb.auth.dependencies import get_current_user
from apowerb.configs.settings import Settings
from apowerb.routers import scheduler as scheduler_router_mod
from apowerb.scheduler.th2etl_client import OrchestratorUnavailable


def _settings(**overrides) -> Settings:
    """⚠️ No orchestrator setting in this base, `api_key` included.

    An earlier version carried `api_key="x"`, copied from a helper where it
    had nothing to do with scheduling. Here it is one of the very fields that
    decides, so leaving it in made every "configured nothing" case
    inexpressible -- the tests below would have been green about a situation
    they never built.
    """
    base = dict(
        working_mode="development",
        encrypt_key="k" * 32,
        rag_webhook_secret="a-real-secret-value",
    )
    base.update(overrides)
    return Settings(**base)


def test_the_base_helper_configures_no_orchestrator():
    """A positive control on the helper itself: if it ever starts providing
    one again, every "nothing configured" test below stops testing anything,
    and this line is what says so."""
    engaged = set(scheduler_router_mod._ORCHESTRATOR_SETTINGS["mage"]) | set(
        scheduler_router_mod._ORCHESTRATOR_SETTINGS["th2etl"]
    )
    assert engaged & _settings().model_fields_set == set()


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------


def test_an_untouched_url_means_no_orchestrator_here():
    assert scheduler_router_mod._orchestrator_is_configured(_settings()) is False


def test_a_named_orchestrator_is_configured():
    cfg = _settings(base_url="http://mage.internal:6789")
    assert scheduler_router_mod._orchestrator_is_configured(cfg) is True


def test_it_reads_the_url_of_the_orchestrator_actually_selected():
    """`ORCHESTRATOR=th2etl` moves the question to `th2etl_base_url`."""
    cfg = _settings(orchestrator="th2etl", th2etl_base_url="http://etl.internal:8009")
    assert scheduler_router_mod._orchestrator_is_configured(cfg) is True


def test_configuring_the_other_one_does_not_count():
    """A deployment on th2etl that only ever set Mage's URL named nothing
    the selected client will use."""
    cfg = _settings(orchestrator="th2etl", base_url="http://mage.internal:6789")
    assert scheduler_router_mod._orchestrator_is_configured(cfg) is False


def test_the_key_the_operator_actually_sets_counts_as_configured():
    """The case that made the first version wrong.

    `scheduler/mage.py` refuses to build a client without `API_KEY`, so that
    is what an operator sets. `BASE_URL` he leaves alone -- the shipped
    default already points at the Mage he just installed beside the API.
    Reading the URL alone called him "not configured", and an outage of his
    Mage would then have gone out at DEBUG, under a message telling him no
    orchestrator was configured.
    """
    cfg = _settings(api_key="a-real-mage-key")
    assert scheduler_router_mod._orchestrator_is_configured(cfg) is True


@pytest.mark.parametrize(
    "field", ["base_url", "api_key", "oauth_token", "project_name", "mage_pipeline_uuid"]
)
def test_any_single_mage_setting_is_enough(field):
    """Engagement, not address. Any one of them says somebody meant to have
    a Mage here."""
    cfg = _settings(**{field: "something"})
    assert scheduler_router_mod._orchestrator_is_configured(cfg) is True


@pytest.mark.parametrize("field", ["th2etl_base_url", "th2etl_api_key"])
def test_any_single_th2etl_setting_is_enough(field):
    cfg = _settings(orchestrator="th2etl", **{field: "something"})
    assert scheduler_router_mod._orchestrator_is_configured(cfg) is True


def test_configuring_mage_does_not_configure_th2etl():
    """Still reads the one actually selected -- a deployment switched to
    th2etl that only ever set Mage's key engaged with nothing its client
    uses."""
    cfg = _settings(orchestrator="th2etl", api_key="a-mage-key", base_url="http://mage:6789")
    assert scheduler_router_mod._orchestrator_is_configured(cfg) is False


@pytest.mark.parametrize("value", ["", "  ", " MAGE ", "Mage"])
def test_a_blank_or_padded_orchestrator_name_still_means_mage(value):
    """The default is `mage`; whitespace and casing are not a third
    orchestrator. Left unhandled, a stray space would have sent the lookup to
    the fallback by accident rather than by decision."""
    cfg = _settings(orchestrator=value, api_key="a-real-mage-key")
    assert scheduler_router_mod._orchestrator_is_configured(cfg) is True


def test_an_unknown_orchestrator_name_falls_back_to_mage_s_url():
    cfg = _settings(orchestrator="something-else", base_url="http://mage.internal:6789")
    assert scheduler_router_mod._orchestrator_is_configured(cfg) is True


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(scheduler_router_mod.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: object()
    return TestClient(app, raise_server_exceptions=False)


class _DeadClient:
    def get_all_pipelines(self):
        raise OrchestratorUnavailable("Mage is unreachable at http://localhost:6789: boom")


class _LiveClient:
    def get_all_pipelines(self):
        return [{"uuid": "agents"}]


def test_a_working_orchestrator_answers_normally(client):
    """Positive control: without it, a 503 everywhere would pass every test
    below for the wrong reason."""
    with patch.object(scheduler_router_mod, "scheduler_client", _LiveClient()):
        got = client.get("/api/pipelines")
    assert got.status_code == 200
    assert got.json() == [{"uuid": "agents"}]


def test_nothing_configured_still_answers_503(client):
    """The contract does not change: the screen must not read this as
    "you have no pipelines"."""
    with patch.object(scheduler_router_mod, "scheduler_client", _DeadClient()), patch.object(
        scheduler_router_mod, "get_settings", lambda: _settings()
    ):
        got = client.get("/api/pipelines")
    assert got.status_code == 503


def test_nothing_configured_is_not_logged_as_an_error(client, caplog):
    with caplog.at_level(logging.ERROR), patch.object(
        scheduler_router_mod, "scheduler_client", _DeadClient()
    ), patch.object(scheduler_router_mod, "get_settings", lambda: _settings()):
        client.get("/api/pipelines")
    assert caplog.records == [], [r.getMessage() for r in caplog.records]


def test_nothing_configured_says_how_to_turn_it_off(client):
    with patch.object(scheduler_router_mod, "scheduler_client", _DeadClient()), patch.object(
        scheduler_router_mod, "get_settings", lambda: _settings()
    ):
        detail = client.get("/api/pipelines").json()["detail"]
    assert "SCHEDULER_ENABLED" in detail
    # The operator never typed this address; quoting it back at him is what
    # made the original message unreadable.
    assert "localhost:6789" not in detail


def test_a_configured_orchestrator_that_is_down_is_still_an_error(client, caplog):
    """The regression this must not cause: a real outage has to stay loud."""
    cfg = _settings(base_url="http://mage.internal:6789")
    with caplog.at_level(logging.ERROR), patch.object(
        scheduler_router_mod, "scheduler_client", _DeadClient()
    ), patch.object(scheduler_router_mod, "get_settings", lambda: cfg):
        got = client.get("/api/pipelines")
    assert got.status_code == 503
    assert len(caplog.records) == 1
    assert "unreachable" in caplog.records[0].getMessage()


def test_a_mage_on_the_default_port_that_dies_is_still_an_error(client, caplog):
    """The regression the first version would have introduced.

    Mage really running beside the API on the documented default port, the
    operator having only ever set `API_KEY` -- and it falls over. That is an
    outage, and it has to be as loud as any other.
    """
    cfg = _settings(api_key="a-real-mage-key")
    with caplog.at_level(logging.ERROR), patch.object(
        scheduler_router_mod, "scheduler_client", _DeadClient()
    ), patch.object(scheduler_router_mod, "get_settings", lambda: cfg):
        got = client.get("/api/pipelines")
    assert got.status_code == 503
    assert len(caplog.records) == 1
    assert "unreachable" in caplog.records[0].getMessage()

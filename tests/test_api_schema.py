"""Production must not publish its own route inventory.

Verified from the outside on 2026-08-06: `/openapi.json` answered 200 with
no credentials on production and on the SCEI deployment, listing 215 and 216
routes with their parameters. Counting the `eval-*` routes ADK 2.x drops was
enough to fingerprint the running ADK version from the internet.

The unauthenticated 401 that guards every other path does not help here:
these routes are added by FastAPI itself, ahead of the auth middleware's
path list.

The tests that matter most are the ones about the *default*. `WORKING_MODE`
lives in each VM's hand-written `.env` and the deploy workflow never sets
it, so a guard that keys on it cannot be trusted to fire on production.
Hence: closed unless explicitly opened.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apowerb.helpers.api_schema import (
    PUBLISH_FLAG,
    SCHEMA_PATHS,
    hide_api_schema,
    publishes_api_schema,
)


class _Settings:
    def __init__(self, working_mode: str | None = None):
        self.working_mode = working_mode


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(PUBLISH_FLAG, raising=False)
    monkeypatch.delenv("WORKING_MODE", raising=False)


def test_an_unconfigured_host_does_not_publish():
    """The whole point: no flag, no schema. Production sets nothing."""
    assert publishes_api_schema(_Settings()) is False


def test_an_unconfigured_host_does_not_publish_even_without_settings():
    assert publishes_api_schema() is False


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on"])
def test_an_explicit_opt_in_publishes(monkeypatch, value):
    monkeypatch.setenv(PUBLISH_FLAG, value)
    assert publishes_api_schema(_Settings("dev")) is True


@pytest.mark.parametrize("value", ["false", "0", "no", "", "  "])
def test_anything_short_of_an_opt_in_stays_closed(monkeypatch, value):
    monkeypatch.setenv(PUBLISH_FLAG, value)
    assert publishes_api_schema(_Settings("dev")) is False


@pytest.mark.parametrize("mode", ["production", "PRODUCTION", "prod", " Prod "])
def test_production_refuses_even_with_the_flag_set(monkeypatch, mode):
    """Two contradictory settings: the safe reading wins."""
    monkeypatch.setenv(PUBLISH_FLAG, "true")
    assert publishes_api_schema(_Settings(mode)) is False


def test_production_is_also_read_from_the_environment(monkeypatch):
    """`settings` may not carry the mode; the variable still counts."""
    monkeypatch.setenv(PUBLISH_FLAG, "true")
    monkeypatch.setenv("WORKING_MODE", "production")
    assert publishes_api_schema(_Settings()) is False


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return app


def test_every_schema_route_stops_answering():
    app = _app()
    client = TestClient(app)
    assert client.get("/openapi.json").status_code == 200

    removed = hide_api_schema(app)

    assert set(removed) <= SCHEMA_PATHS
    assert "/openapi.json" in removed, "the schema itself must be among them"
    for path in SCHEMA_PATHS:
        assert client.get(path).status_code == 404, path


def test_the_rest_of_the_api_is_untouched():
    app = _app()
    hide_api_schema(app)
    assert TestClient(app).get("/health").status_code == 200


def test_the_routes_do_not_come_back_when_fastapi_re_runs_setup():
    """`app.setup()` re-adds them from the `*_url` attributes.

    FastAPI calls it whenever `title`, `version` or `description` is
    reassigned -- an extension doing that would silently undo this guard.
    """
    app = _app()
    hide_api_schema(app)

    app.setup()

    assert TestClient(app).get("/openapi.json").status_code == 404


def test_removing_twice_is_harmless_and_reports_nothing_the_second_time():
    """A no-op must be visible as a no-op, not as a success."""
    app = _app()
    assert hide_api_schema(app)
    assert hide_api_schema(app) == []

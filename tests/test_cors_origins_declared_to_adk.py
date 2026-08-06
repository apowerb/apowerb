"""ADK 2.6 rejects any state-changing request whose Origin it was not told
about, with a 403 raised before authentication. Our front-end and our API sit
on different domains by design, so every direct browser call is cross-origin:
when 2.6.2 was deployed, artifact execution, chat attachments and BI CSV
uploads started failing with "Forbidden: origin not allowed" while GETs kept
working -- an asymmetry that reads as a broken feature, not a CORS policy.

These tests pin both halves of the fix: the origins reach ADK's guard, and
only one CORS layer remains (two would emit Access-Control-Allow-Origin twice,
which browsers reject outright).
"""

from __future__ import annotations

import pytest
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from apowerb.configs.settings import get_settings
from apowerb.main import app

ALLOWED_ORIGIN = get_settings().cors_allowed_origins.split(",")[0].strip()
# Never in the whitelist, whatever the deployment configures.
FOREIGN_ORIGIN = "https://origin-that-is-never-allowed.example"

# A route that exists and requires auth: a 401 proves the request reached the
# application, which is exactly what "the guard let it through" means here.
GUARDED_POST = "/api/files/upload"


def _middleware_classes():
    return [m.cls for m in app.user_middleware]


def test_only_one_cors_layer():
    """ADK installs its own CORSMiddleware as soon as it is given origins.
    Keeping ours as well duplicates Access-Control-Allow-Origin, and a browser
    treats a duplicated value as no value at all."""
    assert _middleware_classes().count(CORSMiddleware) == 1


def test_the_origin_guard_was_told_about_our_origins():
    guard = next(
        (m for m in app.user_middleware if m.cls.__name__ == "_OriginCheckMiddleware"),
        None,
    )
    if guard is None:
        pytest.skip("ADK version without _OriginCheckMiddleware")

    assert guard.kwargs["has_configured_allowed_origins"] is True
    assert ALLOWED_ORIGIN in guard.kwargs["allowed_origins"]


def test_browser_post_from_the_front_end_is_not_blocked():
    """401, not 403: the request reaches authentication instead of dying at
    the origin guard."""
    client = TestClient(app)
    resp = client.post(GUARDED_POST, headers={"Origin": ALLOWED_ORIGIN})
    assert resp.status_code != 403, resp.text


def test_browser_post_from_an_unknown_origin_is_still_blocked():
    """The guard is a CSRF protection, not dead weight -- declaring our own
    origins must not open the door to every other one."""
    client = TestClient(app)
    resp = client.post(GUARDED_POST, headers={"Origin": FOREIGN_ORIGIN})
    assert resp.status_code == 403


def test_preflight_answers_with_a_single_origin_header():
    client = TestClient(app)
    resp = client.options(
        GUARDED_POST,
        headers={
            "Origin": ALLOWED_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["access-control-allow-origin"] == ALLOWED_ORIGIN

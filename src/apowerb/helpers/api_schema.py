"""Whether the API publishes its own schema.

FastAPI serves `/openapi.json`, `/docs` and `/redoc` to anyone by default.
On this platform that hands out the full route inventory -- 215 routes on
production, 216 on the SCEI deployment -- with every path parameter and
response shape, to a caller holding no credentials. It is how the running
ADK version was fingerprinted from the outside on 2026-08-06: counting the
`eval-*` routes that ADK 2.x removed is enough to date the deployment.

These paths were once listed in `ADK_PROTECTED_PATHS` and were commented
out, for a good reason: Swagger UI cannot send a bearer token on its first
load, so requiring one makes the page useless rather than protected. That
is why this module removes the routes instead of guarding them.

## Why an opt-in flag rather than WORKING_MODE

The obvious design is "hide it when WORKING_MODE is production", and it
would in fact fire today: both production VMs carry `WORKING_MODE="prod"`.
That was checked on the machines after this module was written, correcting
an earlier claim in its history that production's value could not be read.

It is still not what this keys on, for a reason that survives the
correction: `WORKING_MODE` lives in each VM's `.env`, written by hand and
never touched by the deploy workflow, which only patches `TH2_EXTENSIONS`.
Nothing keeps it right. A new host, a restored `.env`, a typo -- and a
guard that reads it silently stops guarding, with no signal that anything
changed. Disclosure defaults belong to the code, not to a file no pipeline
owns.

So the default is closed and the exception is explicit: a deployment that
wants a browsable Swagger says so with `PUBLISH_API_SCHEMA=true`. Getting
it wrong costs a developer a page; the other way round costs the route map
of a client's production. `WORKING_MODE=production` still overrides the
flag, as a second lock rather than the only one.
"""

from __future__ import annotations

import os
from typing import Any

# `/docs/oauth2-redirect` is generated from `swagger_ui_oauth2_redirect_url`
# and is easy to forget: leaving it behind serves a page that names the very
# schema the others no longer expose.
SCHEMA_PATHS = frozenset(
    {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
)

PUBLISH_FLAG = "PUBLISH_API_SCHEMA"

_TRUTHY = {"1", "true", "yes", "on"}


def publishes_api_schema(settings: Any = None) -> bool:
    """True only when this deployment explicitly opted in.

    Read from the environment at call time rather than from `settings`: this
    runs once at import, before any settings reload, and the flag has to be
    readable on a host whose `.env` we do not control.

    Production refuses regardless. If `WORKING_MODE` does say production,
    that is a stronger signal than the flag, and an operator who set both
    has contradicted themselves -- the safe reading wins.
    """
    if _is_production(settings):
        return False
    return os.getenv(PUBLISH_FLAG, "").strip().lower() in _TRUTHY


def _is_production(settings: Any) -> bool:
    mode = getattr(settings, "working_mode", None) or os.getenv("WORKING_MODE", "")
    return str(mode).strip().lower() in {"prod", "production"}


def hide_api_schema(app: Any) -> list[str]:
    """Remove the schema and documentation routes from *app*.

    Returns the paths actually removed, so the caller can log what happened
    -- a silent no-op here would be indistinguishable from a working guard
    if FastAPI ever renames these routes.

    The `*_url` attributes are cleared as well: leaving them set lets a
    later `app.setup()` (which FastAPI calls when the title or version is
    reassigned) put the routes straight back.
    """
    removed = [
        route.path
        for route in app.routes
        if getattr(route, "path", None) in SCHEMA_PATHS
    ]
    app.router.routes = [
        route
        for route in app.routes
        if getattr(route, "path", None) not in SCHEMA_PATHS
    ]
    app.openapi_url = None
    app.docs_url = None
    app.redoc_url = None
    app.swagger_ui_oauth2_redirect_url = None
    return removed

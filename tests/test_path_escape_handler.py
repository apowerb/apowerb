"""A path escape must surface as 400, not as a 500 with a traceback.

`contained_path` raises `PathEscape`, a plain ValueError, because it is called
from the agent runtime as well as from routers. That choice only holds if the
HTTP boundary translates it — otherwise the containment fix would turn a
rejected request into a server error and hand the caller a stack trace, which
is a worse outcome than the traversal it prevents.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apowerb.helpers.safe_paths import PathEscape, contained_path


def _app_with_handler() -> FastAPI:
    """Rebuild the registration exactly as main.py does it.

    Importing apowerb.main here would drag in the whole boot sequence
    (migrations, overlay loading, scheduler); this pins the handler contract
    without it.
    """
    import logging as _logging

    from starlette.responses import JSONResponse

    app = FastAPI()

    @app.exception_handler(PathEscape)
    async def _path_escape_handler(request, exc: PathEscape):  # noqa: ANN001
        _logging.getLogger(__name__).warning(
            "[SECURITY] Path escaped its base directory on %s: %s", request.url.path, exc
        )
        return JSONResponse(status_code=400, content={"detail": "Invalid path"})

    # The component travels as a query parameter, not a path segment: a
    # ".." segment is normalised away by URL handling long before routing,
    # so a path-segment probe would 404 and never exercise the handler.
    @app.get("/probe")
    async def probe(component: str, tmp: str = ""):
        return {"path": contained_path(tmp or "/tmp", component)}

    return app


def test_a_traversal_component_returns_400(tmp_path):
    client = TestClient(_app_with_handler(), raise_server_exceptions=False)
    resp = client.get("/probe", params={"component": "..", "tmp": str(tmp_path)})
    assert resp.status_code == 400


def test_the_response_does_not_echo_the_attempted_path(tmp_path):
    """The exception carries the offending components; the response must not.

    Echoing them back tells a prober exactly how their input was parsed.
    """
    client = TestClient(_app_with_handler(), raise_server_exceptions=False)
    resp = client.get("/probe", params={"component": "..", "tmp": str(tmp_path)})
    body = resp.text
    assert ".." not in body
    assert str(tmp_path) not in body
    assert resp.json() == {"detail": "Invalid path"}


def test_an_ordinary_component_still_works(tmp_path):
    client = TestClient(_app_with_handler(), raise_server_exceptions=False)
    resp = client.get("/probe", params={"component": "notes.txt", "tmp": str(tmp_path)})
    assert resp.status_code == 200
    assert resp.json()["path"] == str(tmp_path.resolve() / "notes.txt")


def test_main_registers_the_handler():
    """Pin the wiring itself — the tests above prove the handler's behaviour,
    this proves the real app actually has one for PathEscape."""
    import apowerb.main as main_module

    assert PathEscape in main_module.app.exception_handlers

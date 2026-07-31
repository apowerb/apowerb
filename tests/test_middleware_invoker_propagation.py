"""Proves the load-bearing assumption of the middleware invoker fix:

a ContextVar set inside a Starlette ``BaseHTTPMiddleware`` (before call_next)
IS visible in the downstream endpoint AND inside ``asyncio.to_thread`` (the way
sync tools run). If this holds, binding the invoker in ADKAuthMiddleware
reaches the Outlook tool execution on the ADK-native /run path.
"""

import asyncio

from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.testclient import TestClient

from apowerb.core.invocation_context import (
    set_current_invoker,
    resolve_integration_user,
)


def _build_app():
    app = FastAPI()

    class _BindMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            # Mirror ADKAuthMiddleware: bind the invoker before call_next.
            set_current_invoker("proof@example.com")
            return await call_next(request)

    app.add_middleware(_BindMiddleware)

    @app.get("/run")
    async def _run():
        in_endpoint = resolve_integration_user(prefer_invoker=True)
        # Sync tools run via asyncio.to_thread — verify the copy propagates.
        in_thread = await asyncio.to_thread(
            resolve_integration_user, prefer_invoker=True
        )
        return {"in_endpoint": in_endpoint, "in_thread": in_thread}

    return app


def test_contextvar_set_in_middleware_reaches_endpoint_and_thread():
    set_current_invoker(None)
    client = TestClient(_build_app())
    body = client.get("/run").json()
    assert body["in_endpoint"] == "proof@example.com", body
    assert body["in_thread"] == "proof@example.com", body


def test_two_concurrent_requests_do_not_share_invoker():
    """Different requests must not leak each other's invoker (task isolation)."""
    import threading

    app = FastAPI()
    seen = {}

    class _BindFromHeader(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            set_current_invoker(request.headers.get("x-user"))
            return await call_next(request)

    app.add_middleware(_BindFromHeader)

    @app.get("/who")
    async def _who():
        await asyncio.sleep(0.02)  # widen the interleave window
        return {"user": await asyncio.to_thread(resolve_integration_user, True)}

    client = TestClient(app)

    def _call(user):
        seen[user] = client.get("/who", headers={"x-user": user}).json()["user"]

    threads = [threading.Thread(target=_call, args=(u,)) for u in ("alice", "bob", "carol")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert seen == {"alice": "alice", "bob": "bob", "carol": "carol"}, seen

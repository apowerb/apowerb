"""Tests pour B19 — middleware ``RequestIdMiddleware``.

Chaque requête HTTP doit recevoir un ``request_id`` propagé :

- Si le header ``X-Request-ID`` est absent, le middleware en génère un
  (UUID4 ou nanoid) et l'injecte dans le ``contextvars``.
- Si le header est présent, la valeur fournie par le client est
  utilisée telle quelle (dans la limite raisonnable de longueur).
- La réponse HTTP contient toujours le header ``X-Request-ID`` avec
  la valeur effectivement utilisée pour la requête.
"""

from __future__ import annotations

import re

from fastapi import FastAPI
from fastapi.testclient import TestClient

from th2agent.helpers.request_id_middleware import (
    REQUEST_ID_HEADER,
    RequestIdMiddleware,
    get_request_id,
)


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    @app.get("/ping")
    async def ping():
        # Expose the request_id via body so tests can assert on it.
        return {"request_id": get_request_id()}

    return app


class TestRequestIdMiddleware:
    def test_generates_request_id_when_header_absent(self):
        client = TestClient(_build_app())
        resp = client.get("/ping")
        assert resp.status_code == 200
        header_value = resp.headers.get(REQUEST_ID_HEADER.lower())
        assert header_value is not None and header_value != ""
        # Body's request_id must match the response header.
        body = resp.json()
        assert body["request_id"] == header_value
        # Must look like a reasonable identifier (no whitespace, no newline).
        assert re.fullmatch(r"[A-Za-z0-9_\-]+", header_value)

    def test_reuses_request_id_from_incoming_header(self):
        client = TestClient(_build_app())
        incoming = "client-supplied-abc-123"
        resp = client.get("/ping", headers={REQUEST_ID_HEADER: incoming})
        assert resp.status_code == 200
        assert resp.headers.get(REQUEST_ID_HEADER.lower()) == incoming
        assert resp.json()["request_id"] == incoming

    def test_response_always_contains_request_id_header(self):
        client = TestClient(_build_app())
        # Two calls without header must both be tagged.
        r1 = client.get("/ping")
        r2 = client.get("/ping")
        id1 = r1.headers.get(REQUEST_ID_HEADER.lower())
        id2 = r2.headers.get(REQUEST_ID_HEADER.lower())
        assert id1 and id2
        # Different calls → different ids.
        assert id1 != id2

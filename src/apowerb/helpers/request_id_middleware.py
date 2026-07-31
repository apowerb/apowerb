"""Request ID middleware (B19 / observability).

Every HTTP request is tagged with a short opaque identifier that:

- is read from the incoming ``X-Request-ID`` header if present, or
  generated otherwise (UUID4 without dashes, nanoid-shaped length);
- is exposed to downstream code via a ``contextvars`` so loggers can
  enrich structured log records with it automatically;
- is echoed back on the response as the ``X-Request-ID`` header so
  operators can correlate client ↔ server logs.

Keep this middleware as the outermost wrapper (added last — Starlette
adds middlewares in reverse order) so every other middleware's logs
carry the request_id, including security headers and CORS pre-flights.
"""

from __future__ import annotations

import re
import uuid
from contextvars import ContextVar
from typing import Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


REQUEST_ID_HEADER = "X-Request-ID"

# Accept printable ASCII identifiers up to 128 chars. Anything outside
# that shape is silently replaced by a fresh server-generated id to
# prevent log injection via forged client headers.
_VALID_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")

_request_id_var: ContextVar[Optional[str]] = ContextVar(
    "th2agent_request_id", default=None
)


def _generate_request_id() -> str:
    """Generate a fresh opaque request identifier."""
    return uuid.uuid4().hex


def get_request_id() -> Optional[str]:
    """Return the current request's identifier, or ``None`` outside a request."""
    return _request_id_var.get()


def set_request_id(value: Optional[str]) -> None:
    """Override the current request id (primarily for tests)."""
    _request_id_var.set(value)


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Attach a request id to every request/response pair."""

    def __init__(self, app, header_name: str = REQUEST_ID_HEADER) -> None:
        super().__init__(app)
        self._header = header_name

    async def dispatch(self, request: Request, call_next) -> Response:
        incoming = request.headers.get(self._header)
        if incoming and _VALID_ID_RE.match(incoming):
            request_id = incoming
        else:
            request_id = _generate_request_id()

        token = _request_id_var.set(request_id)
        try:
            response = await call_next(request)
        finally:
            _request_id_var.reset(token)
        response.headers[self._header] = request_id
        return response


__all__ = [
    "REQUEST_ID_HEADER",
    "RequestIdMiddleware",
    "get_request_id",
    "set_request_id",
]

"""HTTP metrics middleware (B19).

Instruments every incoming request with:

- a counter increment (``th2agent_requests_total``) labelled by
  ``method``, normalized ``path`` and ``status``;
- a histogram observation (``th2agent_request_duration_seconds``).

Both metrics degrade to no-ops when ``prometheus_client`` is absent.
"""

from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from apowerb.helpers.metrics import record_request


# Skip instrumentation on the metrics endpoint itself to avoid self-
# referential noise (and because Prometheus scrapers hit it constantly).
_SKIP_PATHS = {"/metrics", "/health/live", "/health/ready"}


class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        if request.url.path in _SKIP_PATHS:
            return await call_next(request)

        start = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            duration = time.perf_counter() - start
            record_request(
                method=request.method,
                path=request.url.path,
                status=status_code,
                duration_s=duration,
            )


__all__ = ["MetricsMiddleware"]

"""Security headers middleware (B8 / H4).

Adds a baseline set of defense-in-depth HTTP response headers on every
request. Values chosen to be compatible with the current frontend
(Next.js SPA on a separate origin) without breaking HTMR/hot reload in
development.

The middleware is intentionally self-contained — no dependency on
settings — so it can be instrumented in unit tests without booting the
full app.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


# Reasonable defaults. Tuned so the Next.js frontend (served from another
# origin) and inline SVG/data: images keep working while blocking obvious
# XSS vectors. Tighten further once a nonce-based CSP is rolled out.
DEFAULT_CSP = (
    "default-src 'self'; "
    "img-src 'self' data: https:; "
    "style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; "
    "connect-src 'self' https: wss:; "
    "font-src 'self' data:; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)

DEFAULT_HSTS = "max-age=31536000; includeSubDomains"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Inject CSP/HSTS/X-Frame-Options/X-Content-Type-Options/Referrer-Policy."""

    def __init__(
        self,
        app,
        *,
        csp: str = DEFAULT_CSP,
        hsts: str = DEFAULT_HSTS,
        frame_options: str = "DENY",
        referrer_policy: str = "no-referrer-when-downgrade",
    ) -> None:
        super().__init__(app)
        self._csp = csp
        self._hsts = hsts
        self._frame_options = frame_options
        self._referrer_policy = referrer_policy

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        # Only set headers if not already present (allow per-route overrides).
        response.headers.setdefault("Content-Security-Policy", self._csp)
        response.headers.setdefault("Strict-Transport-Security", self._hsts)
        response.headers.setdefault("X-Frame-Options", self._frame_options)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", self._referrer_policy)
        return response

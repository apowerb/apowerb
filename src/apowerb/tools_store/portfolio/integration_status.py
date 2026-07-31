"""Structured status codes for integration auth issues.

The auth helpers (``google_auth``, ``microsoft_auth``) raise
:class:`IntegrationStatusError` with one of the codes below when they
cannot produce a usable token. Tools catch the exception and propagate
``error.as_tool_result()`` to the LLM, so the LLM can pick the right
follow-up action without having to parse free-form error strings.

Decision matrix for the LLM (also documented on ``request_integration``):

================================  ===========================================
Code                              LLM should…
================================  ===========================================
``INTEGRATION_MISSING``           Call ``request_integration(provider,
                                  reason=…)`` — the user has never connected.
``INTEGRATION_EXPIRED``           Call ``request_integration(provider, …)`` —
                                  the user must reconnect.
``INTEGRATION_ERROR``             Report the error to the user. Do NOT call
                                  ``request_integration`` — connecting again
                                  will not help (transient API issue, scope
                                  mismatch, server-side outage, etc.).
``INTEGRATION_BLOCKED_BY_TENANT`` Report that the user's organization has
                                  blocked the app at the tenant level (e.g.
                                  SharePoint App Access Policy on Microsoft
                                  365). Reconnecting will NOT help — only an
                                  IT / Microsoft 365 admin can whitelist the
                                  app.
================================  ===========================================
"""

from __future__ import annotations

from typing import Any


INTEGRATION_MISSING = "INTEGRATION_MISSING"
INTEGRATION_EXPIRED = "INTEGRATION_EXPIRED"
INTEGRATION_ERROR = "INTEGRATION_ERROR"
INTEGRATION_BLOCKED_BY_TENANT = "INTEGRATION_BLOCKED_BY_TENANT"


_REMEDIABLE_CODES = frozenset({INTEGRATION_MISSING, INTEGRATION_EXPIRED})


# HTTP status codes used when a FastAPI route surfaces the exception
# directly. ``INTEGRATION_BLOCKED_BY_TENANT`` is referenced by string so
# the mapping stays valid before that constant is introduced — it is
# added in a separate PR for the SharePoint App Access Policy scenario.
# Anything not in the mapping falls back to 401.
_HTTP_STATUS_BY_CODE: dict[str, int] = {
    INTEGRATION_MISSING: 401,           # auth required to fix
    INTEGRATION_EXPIRED: 401,           # auth required to fix
    INTEGRATION_ERROR:   502,           # upstream provider failure
    "INTEGRATION_BLOCKED_BY_TENANT": 403,  # tenant-side authorisation, not auth
}


class IntegrationStatusError(RuntimeError):
    """Auth-layer exception carrying a machine-readable status code.

    Inherits from :class:`RuntimeError` so existing ``except RuntimeError``
    blocks keep working, but tools should add an earlier
    ``except IntegrationStatusError`` branch to return the structured
    result instead of a free-form error string.
    """

    def __init__(self, code: str, provider: str, message: str):
        super().__init__(message)
        self.code = code
        self.provider = provider
        self.message = message

    @property
    def is_remediable_by_reconnect(self) -> bool:
        """True when reconnecting via the Integrations page would fix it."""
        return self.code in _REMEDIABLE_CODES

    @property
    def http_status_code(self) -> int:
        """HTTP status code recommended for FastAPI routes that surface
        this error directly. Defaults to 401 for unknown codes so the
        legacy "auth required" UX still kicks in."""
        return _HTTP_STATUS_BY_CODE.get(self.code, 401)

    def as_tool_result(self) -> dict[str, Any]:
        """Return the dict a tool should hand back to the LLM."""
        return {
            "status": "integration_status",
            "code": self.code,
            "provider": self.provider,
            "message": self.message,
            "remediable_by_reconnect": self.is_remediable_by_reconnect,
            "retry": False,
            "_integration_status": True,
        }

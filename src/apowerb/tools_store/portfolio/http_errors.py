"""Shared mapping of integration exceptions to tool error results.

Turns an exception into ``{"status": "error", "message", "retry"}`` where the
message tells the model what to do next (reconnect, fix the ID, wait) instead
of echoing a raw ``str(exc)``. Used by the SharePoint and Google tools.
"""

from __future__ import annotations

from logging import getLogger

import httpx

from apowerb.tools_store.portfolio.integration_status import IntegrationStatusError

logger = getLogger(__name__)

_BODY_MAX_CHARS = 300


def _google_detail(response: httpx.Response) -> str:
    """Google's ``error.message`` when the body is a standard error JSON."""
    try:
        err = response.json().get("error")
    except Exception:  # noqa: BLE001 — body is not JSON
        return ""
    if isinstance(err, dict):
        return str(err.get("message") or "")[:_BODY_MAX_CHARS]
    return ""


def http_error_result(
    exc: Exception,
    service: str,
    hints: dict[int, str],
    *,
    default_hint: str | None = None,
    with_google_detail: bool = False,
) -> dict:
    """Map ``exc`` to a structured tool error.

    Args:
        exc: The exception caught by the tool.
        service: Label used in messages, e.g. "SharePoint".
        hints: HTTP status -> advice for the model (401/403/404 ...).
        default_hint: Text for a status absent from ``hints``.
        with_google_detail: Append Google's own error message (it names the
            faulty range/ID, which lets the model correct the call).
    """
    if isinstance(exc, IntegrationStatusError):
        return exc.as_tool_result()
    if isinstance(exc, ValueError):
        return {"status": "error", "message": str(exc), "retry": False}
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        logger.warning("[%s] HTTP %s: %s", service, code, exc.response.text[:_BODY_MAX_CHARS])
        message = f"{hints.get(code, default_hint or f'{service} request failed.')} (HTTP {code})"
        if with_google_detail:
            detail = _google_detail(exc.response)
            if detail:
                message += f" {detail}"
        return {
            "status": "error",
            "http_status": code,
            "message": message,
            "retry": code == 429 or code >= 500,
        }
    if isinstance(exc, httpx.TimeoutException):
        return {"status": "error", "message": f"{service} request timed out.", "retry": True}
    logger.warning("[%s] request failed: %s", service, exc)
    return {"status": "error", "message": f"{service} request failed: {exc}", "retry": False}


GOOGLE_HINTS: dict[int, str] = {
    400: "Invalid request: check the ID, range, date format or other parameters.",
    401: "Authentication expired. The user needs to reconnect their Google account.",
    403: "Permission denied: the user cannot access this resource, or the Google API is not enabled/scoped.",
    404: "Not found: check the document, spreadsheet, calendar or event ID.",
    429: "Google rate limit reached; wait a moment before retrying.",
}


def google_error_result(exc: Exception, service: str) -> dict:
    """``http_error_result`` preset for the Google tools."""
    return http_error_result(
        exc, service, GOOGLE_HINTS, default_hint="Google request failed.", with_google_detail=True
    )

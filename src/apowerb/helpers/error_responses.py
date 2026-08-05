"""Turn caught exceptions into client-safe HTTP error payloads.

``str(exc)`` must never reach an HTTP client. It can carry our own
deliberately internal messages (e.g. "ENCRYPT_KEY is not configured —
refusing to persist OAuth tokens without Fernet encryption") or, for
broader ``except Exception`` catches, arbitrary third-party text that may
embed hosts, file paths or connection strings.

Every call site keeps logging the exception in full (with traceback) for
operators; only the text sent back over HTTP is replaced.
"""

from __future__ import annotations

from logging import Logger
from typing import Any


def safe_error_message(
    exc: BaseException,
    *,
    logger: Logger,
    context: str,
    client_message: str,
) -> str:
    """Log *exc* with its traceback under *context*, return *client_message*.

    Use this to build the ``message`` field of an error response (or the
    ``detail`` of an ``HTTPException``) instead of ``str(exc)``.
    """
    logger.error("%s failed: %s", context, exc, exc_info=True)
    return client_message


def sanitize_tool_error(
    result: dict[str, Any],
    *,
    logger: Logger,
    context: str,
    client_message: str,
) -> dict[str, Any]:
    """Replace the ``message`` of a tool-call error dict before it is
    forwarded verbatim as an HTTP response body.

    Tool functions (``tool_list_files`` and friends) are shared with the
    ADK agent path, where the raw message is useful to the LLM — some of
    their error branches embed ``str(exc)`` from a bare ``except
    Exception``. That is fine for the agent, not for an HTTP client, so
    browser routers must sanitize the dict before returning it.

    Non-error results (or anything that isn't an error dict) pass through
    unchanged.
    """
    if not isinstance(result, dict) or result.get("status") != "error":
        return result
    logger.error("%s returned an error: %s", context, result.get("message"))
    return {**result, "message": client_message}

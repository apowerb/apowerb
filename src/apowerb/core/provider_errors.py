"""Model-provider failures, turned into a category the caller can act on.

When the LLM provider refuses a call (revoked key, quota), LiteLLM raises and
the exception used to leave ``/run`` as an anonymous ``500 Internal Server
Error``: the chat, the workflow engine and API clients could only say
"internal error", although the cause is known and fixable by the user.

The provider's own message is never sent back: it can carry URLs, account
identifiers or pieces of a key. The client gets a stable ``code`` and a
``ref``; the full exception is logged under that ``ref``.
"""

from __future__ import annotations

import uuid
from logging import getLogger
from typing import Optional

from fastapi.responses import JSONResponse

logger = getLogger(__name__)

MODEL_PROVIDER_AUTH = "model_provider_auth"
MODEL_PROVIDER_RATE_LIMIT = "model_provider_rate_limit"
MODEL_PROVIDER_UNAVAILABLE = "model_provider_unavailable"

# code -> (HTTP status returned by /run, English fallback message)
_RESPONSES = {
    MODEL_PROVIDER_AUTH: (
        502,
        "The model provider rejected the credentials of this agent's model.",
    ),
    MODEL_PROVIDER_RATE_LIMIT: (
        429,
        "The model provider is rate limiting this agent's model.",
    ),
    MODEL_PROVIDER_UNAVAILABLE: (
        502,
        "The model provider could not be reached.",
    ),
}


def _exception_classes() -> dict[type[BaseException], Optional[str]]:
    """LiteLLM exceptions answered by this module, with their category.

    ``APIError`` has none of its own: LiteLLM falls back to it when the
    provider's error body is not the one it expects (OVH AI Endpoints answers a
    revoked key that way), and only its HTTP status tells what happened.
    """
    import litellm

    ex = litellm.exceptions
    return {
        ex.AuthenticationError: MODEL_PROVIDER_AUTH,
        ex.PermissionDeniedError: MODEL_PROVIDER_AUTH,
        ex.RateLimitError: MODEL_PROVIDER_RATE_LIMIT,
        ex.ServiceUnavailableError: MODEL_PROVIDER_UNAVAILABLE,
        ex.APIConnectionError: MODEL_PROVIDER_UNAVAILABLE,
        ex.APIError: None,
    }


def _code_from_status(exc: BaseException) -> Optional[str]:
    status = getattr(exc, "status_code", None)
    if status in (401, 403):
        return MODEL_PROVIDER_AUTH
    if status == 429:
        return MODEL_PROVIDER_RATE_LIMIT
    if isinstance(status, int) and status >= 500:
        return MODEL_PROVIDER_UNAVAILABLE
    return None


def provider_error_code(exc: BaseException) -> Optional[str]:
    """The category of a provider failure found in ``exc`` or its causes."""
    classes = _exception_classes()
    seen: Optional[BaseException] = exc
    visited: set[int] = set()
    while seen is not None and id(seen) not in visited:
        visited.add(id(seen))
        for cls, code in classes.items():
            if isinstance(seen, cls):
                found = code or _code_from_status(seen)
                if found:
                    return found
        group = getattr(seen, "exceptions", None)
        if isinstance(group, (list, tuple)):
            for inner in group:
                found = provider_error_code(inner)
                if found:
                    return found
        seen = seen.__cause__ or seen.__context__
    return None


def provider_error_response(exc: BaseException, code: str) -> JSONResponse:
    status, message = _RESPONSES[code]
    ref = uuid.uuid4().hex[:8]
    logger.error("[provider] %s ref=%s : %r", code, ref, exc, exc_info=exc)
    return JSONResponse(
        status_code=status,
        content={"detail": message, "code": code, "ref": ref},
    )


def register_provider_error_handlers(app) -> None:  # noqa: ANN001
    """Answer provider failures with their category instead of a bare 500."""

    async def _handler(request, exc: BaseException):  # noqa: ANN001
        code = provider_error_code(exc)
        if code is None:
            # Not a provider refusal we can name (a 400 on a bad request, for
            # instance): same outcome as without this module.
            raise exc
        return provider_error_response(exc, code)

    for cls in _exception_classes():
        app.add_exception_handler(cls, _handler)

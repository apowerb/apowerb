"""Vérification des JWT OIDC émis par Google (Pub/Sub push, etc.).

Google Cloud Pub/Sub signe ses notifications push avec un JWT OIDC
placé dans l'en-tête ``Authorization: Bearer <token>``. Ce module
encapsule la vérification de cryptographique + des claims standards
(``iss``, ``aud``, ``iat``, ``exp``) pour les endpoints webhook.

Lève ``HTTPException`` directement afin que les handlers FastAPI
puissent simplement laisser l'exception remonter.
"""

from __future__ import annotations

from logging import getLogger
from typing import Any, Mapping

from fastapi import HTTPException, status
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

logger = getLogger(__name__)

_GOOGLE_ISSUERS = frozenset(
    {"https://accounts.google.com", "accounts.google.com"}
)


def _extract_bearer_token(authorization_header: str | None) -> str:
    """Extract the bearer token from an ``Authorization`` header value.

    Raises ``HTTPException(401)`` if the header is missing or not a bearer.
    """
    if not authorization_header:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
        )

    scheme, _, token = authorization_header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Authorization scheme (expected Bearer)",
        )

    return token.strip()


def verify_gmail_push_jwt(
    authorization_header: str | None,
    audience: str,
) -> Mapping[str, Any]:
    """Verify a Gmail / Pub/Sub OIDC push JWT and return its decoded claims.

    Args:
        authorization_header: raw value of the ``Authorization`` HTTP header
            (``"Bearer <jwt>"``). May be ``None``.
        audience: expected ``aud`` claim. Typically the full webhook URL
            or a custom audience configured on the Pub/Sub subscription.

    Returns:
        The decoded token claims on success.

    Raises:
        HTTPException: 401 if header is missing/malformed ;
            403 if the JWT signature is invalid, expired, wrong audience,
            or wrong issuer.
    """
    if not audience:
        # Misconfiguration: refuse rather than accept an unverified audience.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Webhook audience is not configured",
        )

    token = _extract_bearer_token(authorization_header)

    try:
        claims = id_token.verify_oauth2_token(
            token,
            google_requests.Request(),
            audience=audience,
        )
    except ValueError as exc:
        # google-auth raises ValueError for bad signature / expired / wrong aud
        logger.warning("[OIDC] Gmail webhook JWT rejected: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid OIDC token",
        ) from exc
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("[OIDC] Unexpected error while verifying JWT: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid OIDC token",
        ) from exc

    issuer = claims.get("iss")
    if issuer not in _GOOGLE_ISSUERS:
        logger.warning("[OIDC] Rejected JWT with non-Google issuer: %s", issuer)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid token issuer",
        )

    return claims

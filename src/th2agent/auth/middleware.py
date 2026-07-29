from fastapi import Request, HTTPException, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from typing import Optional
import re


class AuthMiddleware(BaseHTTPMiddleware):
    """
    Middleware to authenticate requests using Bearer token or API Key
    """

    # Paths that don't require authentication
    EXCLUDED_PATHS = [
        "/docs",
        "/redoc",
        "/openapi.json",
        "/api/auth/login",
        "/api/auth/register",
        "/api/auth/refresh",
        "/health",
        "/api/users/",
    ]

    # Regex patterns for excluded paths (W-05: handles trailing slash)
    EXCLUDED_PATTERNS = [
        r"^/static/.*",
        r"^/assets/.*",
        r"^/api/auth/mfa/verify/?$",
    ]

    async def dispatch(self, request: Request, call_next):
        # Check if path should be excluded
        if self._is_excluded_path(request.url.path):
            return await call_next(request)

        # Try to get authentication token
        token = self._extract_token(request)

        if not token:
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "Authentication required"},
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Validate token
        try:
            user = await self._validate_token(token)
            # Add user to request state for downstream use
            request.state.user = user
        except HTTPException as e:
            return JSONResponse(
                status_code=e.status_code,
                content={"detail": e.detail},
            )
        except Exception:
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "Invalid authentication credentials"},
            )

        response = await call_next(request)
        return response

    def _is_excluded_path(self, path: str) -> bool:
        """Check if path should be excluded from authentication"""
        # Check exact matches
        if path in self.EXCLUDED_PATHS:
            return True

        # Check regex patterns
        for pattern in self.EXCLUDED_PATTERNS:
            if re.match(pattern, path):
                return True

        return False

    def _extract_token(self, request: Request) -> Optional[str]:
        """Extract token from Authorization header or X-API-Key header"""
        # Try Bearer token first
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            return auth_header.split(" ")[1]

        # Try API Key
        api_key = request.headers.get("X-API-Key")
        if api_key:
            return api_key

        return None

    async def _validate_token(self, token: str):
        """Validate token and return user info"""
        # Import here to avoid circular imports
        from th2agent.helpers.security import decode_access_token

        try:
            payload = decode_access_token(token)
            return payload
        except RuntimeError:
            # Clé de signature absente : panne de configuration serveur. La
            # convertir en 401 accuserait l'appelant d'un problème qui n'est
            # pas le sien — et masquerait la vraie cause.
            raise
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "Invalid or expired token"},
            )

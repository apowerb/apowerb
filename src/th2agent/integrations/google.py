import httpx
from typing import Optional, Dict
from urllib.parse import urlencode
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from logging import getLogger

from th2agent.configs.settings import get_settings
from th2agent.helpers.encryptor import decrypt_value
from th2agent.integrations.helpers import save_integration_tokens
from th2agent.models import Integration

logger = getLogger(__name__)
settings = get_settings()

# Google OAuth endpoints
_GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"

# Service-specific scopes.  Each key is a distinct provider stored in the
# integrations table so a user can connect Drive, Gmail *and* Calendar
# independently (each with its own token + scopes).
GOOGLE_SERVICES: Dict[str, Dict[str, str]] = {
    "google_drive": {
        "label": "Google Drive",
        "scopes": "openid email profile https://www.googleapis.com/auth/drive.readonly",
    },
    "google_gmail": {
        "label": "Gmail",
        "scopes": "openid email profile https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.send",
    },
    "google_calendar": {
        "label": "Google Calendar",
        "scopes": "openid email profile https://www.googleapis.com/auth/calendar.readonly https://www.googleapis.com/auth/calendar.events",
    },
    "google_sheets": {
        "label": "Google Sheets",
        "scopes": "openid email profile https://www.googleapis.com/auth/spreadsheets",
    },
    "google_docs": {
        "label": "Google Docs",
        "scopes": "openid email profile https://www.googleapis.com/auth/documents",
    },
}


class GoogleIntegrationService:
    """All methods are static — no instance state needed."""

    # 1 — Build the Google OAuth authorisation URL

    @staticmethod
    def get_oauth_url(
        service: str,
        state: str,
        redirect_uri: str | None = None,
    ) -> str:
        """
        Build the Google OAuth consent URL for a specific service.

        Args:
            service: One of the keys in GOOGLE_SERVICES (e.g. "google_drive").
            state: A cryptographically random string generated per request.
            redirect_uri: Optional override for the callback URL.

        Returns:
            Full Google authorisation URL as a string.
        """
        scopes = GOOGLE_SERVICES[service]["scopes"]
        params = {
            "client_id": settings.google_integration_client_id,
            "redirect_uri": redirect_uri or settings.google_integration_redirect_uri,
            "response_type": "code",
            "scope": scopes,
            "state": state,
            "access_type": "offline",
            "prompt": "consent",
        }
        return f"{_GOOGLE_AUTHORIZE_URL}?{urlencode(params)}"

    # 2 — Exchange the temporary code for tokens

    @staticmethod
    async def exchange_code_for_token(
        code: str,
        redirect_uri: str | None = None,
    ) -> Optional[Dict]:
        """
        POST the temporary OAuth code to Google and get access + refresh tokens.

        Returns:
            Dict with at minimum ``access_token``, ``refresh_token``, and ``scope``,
            or None on failure.
        """
        async with httpx.AsyncClient() as client:
            response = await client.post(
                _GOOGLE_TOKEN_URL,
                data={
                    "client_id": settings.google_integration_client_id,
                    "client_secret": settings.google_integration_client_secret,
                    "code": code,
                    "redirect_uri": redirect_uri or settings.google_integration_redirect_uri,
                    "grant_type": "authorization_code",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=15.0,
            )

        if response.status_code != 200:
            logger.warning(
                "Google integration token exchange failed: HTTP %s — %s",
                response.status_code,
                response.text,
            )
            return None

        data = response.json()

        if "error" in data:
            logger.warning(
                "Google integration token exchange returned error: %s — %s",
                data.get("error"),
                data.get("error_description"),
            )
            return None

        return data  # {"access_token": "...", "refresh_token": "...", "scope": "...", ...}

    # 3 — Fetch the Google user profile

    @staticmethod
    async def get_google_user(access_token: str) -> Optional[Dict]:
        """
        Call GET /oauth2/v2/userinfo on Google with the integration token.

        Returns:
            Google user dict, or None if the call fails.
        """
        async with httpx.AsyncClient() as client:
            response = await client.get(
                _GOOGLE_USERINFO_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                },
                timeout=10.0,
            )

        if response.status_code != 200:
            logger.warning(
                "Failed to fetch Google user info: HTTP %s — %s",
                response.status_code,
                response.text,
            )
            return None

        return response.json()

    # 4 — Persist / update the integration row

    @staticmethod
    async def save_integration(
        db: AsyncSession,
        user_id: int,
        google_data: Dict,
        access_token: str,
        refresh_token: str = "",
        scopes: str = "",
        service: str = "google_drive",
    ) -> Integration:
        """
        Upsert the Google integration for a user and specific service.

        Tokens are Fernet-encrypted at rest via
        :func:`th2agent.integrations.helpers.save_integration_tokens`.
        """
        email = google_data.get("email", "")

        profile_meta = {
            "picture": google_data.get("picture"),
            "name": google_data.get("name"),
            "locale": google_data.get("locale"),
        }

        save_integration_tokens(
            user_id=user_id,
            provider=service,
            access_token=access_token,
            refresh_token=refresh_token or None,
            provider_username=email,
            provider_user_id=str(google_data.get("id", "")),
            scopes=scopes,
            meta=profile_meta,
        )
        logger.info(
            "Persisted %s integration (encrypted) for user_id=%s (email=%s)",
            service, user_id, email,
        )

        result = await db.execute(
            select(Integration).where(
                Integration.user_id == user_id,
                Integration.provider == service,
            )
        )
        integration = result.scalar_one()
        return integration

    # Helper — retrieve the stored token for a user (for tool usage)

    @staticmethod
    async def get_user_token(
        db: AsyncSession,
        user_id: int,
        service: str = "google_drive",
    ) -> Optional[str]:
        """Return the plaintext Google access token or None."""
        result = await db.execute(
            select(Integration).where(
                Integration.user_id == user_id,
                Integration.provider == service,
            )
        )
        integration = result.scalar_one_or_none()
        if not integration or not integration.access_token:
            return None
        from cryptography.fernet import InvalidToken
        try:
            return decrypt_value(integration.access_token)
        except InvalidToken:
            logger.warning(
                "%s access_token for user_id=%s is plaintext (pre-B7) — "
                "run the integration token migration CLI.",
                service, user_id,
            )
            return integration.access_token
        except Exception as exc:
            logger.warning(
                "Failed to decrypt %s access_token for user_id=%s: %s",
                service, user_id, exc,
            )
            return None

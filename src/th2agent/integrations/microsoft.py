import base64
import json
import time
import httpx
from typing import Optional, Dict
from urllib.parse import urlencode
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from logging import getLogger

from th2agent.configs.settings import get_settings
from th2agent.helpers.encryptor import decrypt_value, encrypt_value
from th2agent.integrations.helpers import save_integration_tokens
from th2agent.models import Integration

logger = getLogger(__name__)
settings = get_settings()


class IntegrationTokenExpiredError(Exception):
    """Raised when a Microsoft integration's refresh token is missing, revoked, or expired.

    The caller (router) should map this to HTTP 401 and tell the user to reconnect.
    """


def _decode_jwt_exp(token: str) -> Optional[int]:
    """Return the ``exp`` claim of a JWT without verifying its signature.

    Returns None if the token is malformed.
    """
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = claims.get("exp")
        return int(exp) if exp is not None else None
    except Exception:
        return None


def is_access_token_expired(token: Optional[str], leeway_seconds: int = 60) -> bool:
    """True if the token is missing, malformed, or will expire within ``leeway_seconds``."""
    if not token:
        return True
    exp = _decode_jwt_exp(token)
    if exp is None:
        return True
    return time.time() + leeway_seconds >= exp

# Microsoft OAuth endpoints
_MS_AUTHORIZE_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize"
_MS_TOKEN_URL     = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
_MS_GRAPH_ME_URL  = "https://graph.microsoft.com/v1.0/me"

# ── Identity — included in every service bundle ───────────────────────────────
# offline_access  – silent token renewal (no re-auth prompts)
# openid          – confirms identity
# profile         – name, display name, job title
# email           – email address only
# User.Read       – read /me from Graph (no write)
_IDENTITY = "offline_access openid profile email User.Read"

# Per-service scope bundles — each service only gets the permissions it needs.
MICROSOFT_SERVICE_SCOPES: dict[str, str] = {
    # Mail.Read  – read emails, NO delete/move/flag
    # Mail.Send  – send only (drafts go to Sent, nothing else touched)
    "outlook":    f"{_IDENTITY} Mail.Read Mail.Send Mail.Read.Shared Mail.Send.Shared",
    # Chat.Read  – read chats only, no sending
    "teams":      f"{_IDENTITY} Chat.Read Chat.ReadWrite ChatMessage.Send",
    # Sites.Read.All – read sites/lists, no mutations
    "sharepoint": f"{_IDENTITY} Sites.Read.All",
    # Files.Read – read only the user's own files (not .All)
    "onedrive":   f"{_IDENTITY} Files.Read Files.ReadWrite",
}

SUPPORTED_MICROSOFT_SERVICES: list[str] = list(MICROSOFT_SERVICE_SCOPES.keys())


def _scopes_for(service: str) -> str:
    """Return the scope string for the requested service.
    Falls back to outlook scopes for unknown keys."""
    scopes = MICROSOFT_SERVICE_SCOPES.get(service)
    if scopes is None:
        logger.warning("Unknown Microsoft service '%s' — falling back to outlook scopes.", service)
        scopes = MICROSOFT_SERVICE_SCOPES["outlook"]
    return scopes


def _provider_name(service: str) -> str:
    """DB provider string, e.g. 'outlook' → 'microsoft_outlook'."""
    return f"microsoft_{service}"


class MicrosoftIntegrationService:
    """All methods are static — no instance state needed."""

    # Helpers
    @staticmethod
    def _authorize_url() -> str:
        return _MS_AUTHORIZE_URL.format(tenant=settings.microsoft_integration_tenant_id)

    @staticmethod
    def _token_url() -> str:
        return _MS_TOKEN_URL.format(tenant=settings.microsoft_integration_tenant_id)

    # 1 — Build the Microsoft OAuth authorisation URL

    @staticmethod
    def get_oauth_url(state: str, service: str = "outlook", redirect_uri: str | None = None) -> str:
        """
        Build the Microsoft OAuth consent URL for a specific service.

        Args:
            state:        A cryptographically random string generated per request.
            service:      One of SUPPORTED_MICROSOFT_SERVICES. Determines which scopes are requested.
            redirect_uri: Optional override. Falls back to settings.microsoft_integration_redirect_uri.

        Returns:
            Full Microsoft authorisation URL as a string.
        """
        params = {
            "client_id":     settings.microsoft_integration_client_id,
            "response_type": "code",
            "redirect_uri":  redirect_uri or settings.microsoft_integration_redirect_uri,
            "response_mode": "query",
            "scope":         _scopes_for(service),
            "state":         state,
            "prompt":        "select_account",
        }
        return f"{MicrosoftIntegrationService._authorize_url()}?{urlencode(params)}"

    # 2 — Exchange the temporary code for tokens

    @staticmethod
    async def exchange_code_for_token(
        code: str,
        service: str = "outlook",
        redirect_uri: str | None = None,
    ) -> Optional[Dict]:
        """
        POST the temporary OAuth code to Microsoft and get access + refresh tokens.

        Args:
            code:         Temporary authorisation code from Microsoft.
            service:      Service key — must match what was sent in the auth request.
            redirect_uri: Must exactly match the URI used in get_oauth_url.

        Returns:
            Dict with at minimum ``access_token``, ``refresh_token``, and ``scope``,
            or None on failure.
        """
        async with httpx.AsyncClient() as client:
            response = await client.post(
                MicrosoftIntegrationService._token_url(),
                data={
                    "client_id":     settings.microsoft_integration_client_id,
                    "client_secret": settings.microsoft_integration_client_secret,
                    "code":          code,
                    "redirect_uri":  redirect_uri or settings.microsoft_integration_redirect_uri,
                    "grant_type":    "authorization_code",
                    "scope":         _scopes_for(service),
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=15.0,
            )

        if response.status_code != 200:
            logger.warning(
                "Microsoft %s token exchange failed: HTTP %s — %s",
                service,
                response.status_code,
                response.text,
            )
            return None

        data = response.json()

        if "error" in data:
            logger.warning(
                "Microsoft %s token exchange returned error: %s — %s",
                service,
                data.get("error"),
                data.get("error_description"),
            )
            return None

        return data  # {"access_token": "...", "refresh_token": "...", "scope": "...", ...}

    # 3 — Fetch the Microsoft Graph user profile

    @staticmethod
    async def get_microsoft_user(access_token: str) -> Optional[Dict]:
        """
        Call GET /me on Microsoft Graph with the integration token.

        Returns:
            Graph user dict, or None if the call fails.
        """
        async with httpx.AsyncClient() as client:
            response = await client.get(
                _MS_GRAPH_ME_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept":        "application/json",
                },
                timeout=10.0,
            )

        if response.status_code != 200:
            logger.warning(
                "Failed to fetch Microsoft user info: HTTP %s — %s",
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
        ms_data: Dict,
        access_token: str,
        refresh_token: str = "",
        scopes: str = "",
        service: str = "outlook",
    ) -> Integration:
        """
        Upsert the Microsoft integration for a given service.

        Tokens are Fernet-encrypted at rest via
        :func:`th2agent.integrations.helpers.save_integration_tokens`.
        """
        provider = _provider_name(service)

        email        = ms_data.get("userPrincipalName") or ms_data.get("mail") or ""
        display_name = ms_data.get("displayName", "")

        # Preserve user-managed meta keys across reconnects (the Microsoft Graph
        # profile does not return them, and save_integration_tokens overwrites
        # the meta column wholesale).
        existing = await db.execute(
            select(Integration.meta).where(
                Integration.user_id == user_id,
                Integration.provider == provider,
            )
        )
        existing_meta = existing.scalar_one_or_none() or {}
        preserved_meta = {
            k: existing_meta[k]
            for k in ("shared_mailboxes", "active_shared_mailbox")
            if k in existing_meta
        }

        profile_meta = {
            **preserved_meta,
            "display_name": display_name,
            "given_name":   ms_data.get("givenName"),
            "surname":      ms_data.get("surname"),
            "job_title":    ms_data.get("jobTitle"),
            "office":       ms_data.get("officeLocation"),
        }

        save_integration_tokens(
            user_id=user_id,
            provider=provider,
            access_token=access_token,
            refresh_token=refresh_token or None,
            provider_username=email,
            provider_user_id=ms_data.get("id", ""),
            scopes=scopes,
            meta=profile_meta,
        )
        logger.info(
            "Persisted Microsoft %s integration (encrypted) for user_id=%s (email=%s)",
            service, user_id, email,
        )

        result = await db.execute(
            select(Integration).where(
                Integration.user_id == user_id,
                Integration.provider == provider,
            )
        )
        integration = result.scalar_one()
        return integration

    # Helper — retrieve the stored token for a user (for tool usage)

    @staticmethod
    async def get_user_token(
        db: AsyncSession,
        user_id: int,
        service: str = "outlook",
    ) -> Optional[str]:
        """Return the plaintext Microsoft access token, or None."""
        result = await db.execute(
            select(Integration).where(
                Integration.user_id == user_id,
                Integration.provider == _provider_name(service),
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
                "Microsoft %s access_token for user_id=%s is plaintext (pre-B7) — "
                "run the integration token migration CLI.",
                service, user_id,
            )
            return integration.access_token
        except Exception as exc:
            logger.warning(
                "Failed to decrypt Microsoft %s access_token for user_id=%s: %s",
                service, user_id, exc,
            )
            return None

    # Helper — return a *valid* token, refreshing via refresh_token if needed

    @staticmethod
    async def get_valid_access_token(
        db: AsyncSession,
        user_id: int,
        service: str = "outlook",
    ) -> str:
        """Return a non-expired Microsoft access token for the given user+service.

        If the stored access token is expired (or about to expire), silently
        exchange the refresh_token for a fresh access_token and persist it.

        Raises:
            IntegrationTokenExpiredError: if no integration exists, no refresh
                token is stored, or the refresh call fails (invalid_grant, …).
                The caller should map this to HTTP 401 + "reconnect Outlook".
        """
        result = await db.execute(
            select(Integration).where(
                Integration.user_id == user_id,
                Integration.provider == _provider_name(service),
            )
        )
        integration = result.scalar_one_or_none()
        if not integration:
            raise IntegrationTokenExpiredError(
                f"No {service} integration found for user_id={user_id}"
            )

        try:
            access_token = decrypt_value(integration.access_token) if integration.access_token else ""
        except Exception:
            access_token = ""

        if not is_access_token_expired(access_token):
            return access_token

        try:
            refresh_token = decrypt_value(integration.refresh_token) if integration.refresh_token else ""
        except Exception:
            refresh_token = ""
        if not refresh_token:
            raise IntegrationTokenExpiredError(
                f"{service} access token expired and no refresh token available."
            )

        logger.info(
            "[%s] access_token expired for user_id=%s — refreshing via refresh_token",
            _provider_name(service), user_id,
        )

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                MicrosoftIntegrationService._token_url(),
                data={
                    "client_id":     settings.microsoft_integration_client_id,
                    "client_secret": settings.microsoft_integration_client_secret,
                    "refresh_token": refresh_token,
                    "grant_type":    "refresh_token",
                    "scope":         _scopes_for(service),
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=15.0,
            )

        if resp.status_code != 200:
            logger.warning(
                "[%s] refresh_token exchange failed (HTTP %s): %s",
                _provider_name(service), resp.status_code, resp.text[:300],
            )
            raise IntegrationTokenExpiredError(
                f"{service} refresh_token is invalid or revoked — user must reconnect."
            )

        data = resp.json()
        new_access = data.get("access_token")
        if not new_access:
            raise IntegrationTokenExpiredError(
                f"{service} token endpoint returned no access_token."
            )

        # Persist the new access token (and new refresh token if Microsoft rotated it).
        integration.access_token = encrypt_value(new_access)  # type: ignore[assignment]
        new_refresh = data.get("refresh_token")
        if new_refresh:
            integration.refresh_token = encrypt_value(new_refresh)  # type: ignore[assignment]
        if data.get("scope"):
            integration.scopes = data["scope"]  # type: ignore[assignment]
        await db.commit()
        await db.refresh(integration)

        return new_access

    @staticmethod
    def remove_outlook_tool_config(owner_email: str) -> None:
        """Delete the Outlook Mail tool_config for a user.

        Called automatically when the Microsoft integration is disconnected so
        the agent tools are disabled in lockstep with the integration removal.
        Errors are logged but never re-raised — the integration row deletion
        is the primary action and must not be blocked.
        """
        try:
            delete_tool_config_by_owner_and_category(owner_email, _OUTLOOK_TOOL_CATEGORY)
            logger.info(
                "Outlook Mail tool_config removed for owner=%s", owner_email
            )
        except Exception as exc:
            logger.warning(
                "Failed to remove Outlook Mail tool_config for owner=%s: %s",
                owner_email, exc,
            )

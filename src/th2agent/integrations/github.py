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

# GitHub OAuth endpoints
_GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
_GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
_GITHUB_API_URL = "https://api.github.com"

# Scopes requested for the integration token.
# repo        – read/write access to repositories
# read:user   – read the user's profile
# user:email  – read the user's email address
INTEGRATION_SCOPES = "repo,read:user,user:email"


class GitHubIntegrationService:
    """All methods are static — no instance state needed."""

    #1 — Build the GitHub OAuth authorisation URL
    @staticmethod
    def get_oauth_url(state: str, redirect_uri: str | None = None) -> str:
        """
        Build the GitHub OAuth redirect URL.

        Args:
            state: A cryptographically random string generated per request.
                   The frontend must pass it back in the callback so we can
                   verify it was not tampered with.
            redirect_uri: Optional override for the callback URL. When provided
                          (e.g. by the frontend passing its own origin) it takes
                          precedence over the value in settings. This allows dev
                          and prod deployments to each redirect back to their own
                          origin without separate .env files.

        Returns:
            Full GitHub authorisation URL as a string.
        """
        params = {
            "client_id": settings.github_integration_client_id,
            "redirect_uri": redirect_uri or settings.github_integration_redirect_uri,
            "scope": INTEGRATION_SCOPES,
            "state": state,
        }
        return f"{_GITHUB_AUTHORIZE_URL}?{urlencode(params)}"

    #2 — Exchange the temporary code for an access token
    @staticmethod
    async def exchange_code_for_token(code: str) -> Optional[Dict]:
        """
        POST the temporary OAuth code to GitHub and get an access token.

        Returns:
            Dict with at minimum `access_token` and `scope`, or None on failure.
        """
        async with httpx.AsyncClient() as client:
            response = await client.post(
                _GITHUB_TOKEN_URL,
                json={
                    "client_id": settings.github_integration_client_id,
                    "client_secret": settings.github_integration_client_secret,
                    "code": code,
                    "redirect_uri": settings.github_integration_redirect_uri,
                },
                headers={"Accept": "application/json"},
                timeout=15.0,
            )

        if response.status_code != 200:
            logger.warning(
                "GitHub integration token exchange failed: HTTP %s — %s",
                response.status_code,
                response.text,
            )
            return None

        data = response.json()

        if "error" in data:
            logger.warning(
                "GitHub integration token exchange returned error: %s — %s",
                data.get("error"),
                data.get("error_description"),
            )
            return None

        return data  # {"access_token": "...", "token_type": "bearer", "scope": "..."}

    #3 — Fetch the GitHub user profile
    @staticmethod
    async def get_github_user(access_token: str) -> Optional[Dict]:
        """
        Call GET /user on the GitHub API with the integration token.

        Returns:
            GitHub user dict, or None if the call fails.
        """
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{_GITHUB_API_URL}/user",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=10.0,
            )

        if response.status_code != 200:
            logger.warning(
                "Failed to fetch GitHub user info: HTTP %s",
                response.status_code,
            )
            return None

        return response.json()

    #4 — Persist / update the integration row
    @staticmethod
    async def save_integration(
        db: AsyncSession,
        user_id: int,
        github_data: Dict,
        access_token: str,
        scopes: str = "",
    ) -> Integration:
        """
        Upsert the GitHub integration for a user.

        The access token is Fernet-encrypted at rest via
        :func:`th2agent.integrations.helpers.save_integration_tokens`.

        Returns:
            The persisted Integration ORM object (with ciphertext columns).
        """
        profile_meta = {
            "avatar_url": github_data.get("avatar_url"),
            "name": github_data.get("name"),
            "html_url": github_data.get("html_url"),
            "bio": github_data.get("bio"),
            "public_repos": github_data.get("public_repos"),
        }

        save_integration_tokens(
            user_id=user_id,
            provider="github",
            access_token=access_token,
            refresh_token=None,
            provider_username=github_data.get("login"),
            provider_user_id=str(github_data.get("id", "")),
            scopes=scopes,
            meta=profile_meta,
        )
        logger.info(
            "Persisted GitHub integration (encrypted) for user_id=%s (login=%s)",
            user_id,
            github_data.get("login"),
        )

        result = await db.execute(
            select(Integration).where(
                Integration.user_id == user_id,
                Integration.provider == "github",
            )
        )
        integration = result.scalar_one()
        return integration

    # Helper — retrieve the stored token for a user (for tool usage)
    @staticmethod
    async def get_user_token(db: AsyncSession, user_id: int) -> Optional[str]:
        """
        Retrieve the decrypted GitHub access token for a user.

        Used by tools that need to call the GitHub API on behalf of the user.

        Returns:
            The plaintext access_token string, or None if no integration exists.
        """
        result = await db.execute(
            select(Integration).where(
                Integration.user_id == user_id,
                Integration.provider == "github",
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
                "GitHub access_token for user_id=%s is plaintext (pre-B7) — "
                "run the integration token migration CLI.",
                user_id,
            )
            return integration.access_token
        except Exception as exc:
            logger.warning(
                "Failed to decrypt GitHub access_token for user_id=%s: %s",
                user_id, exc,
            )
            return None
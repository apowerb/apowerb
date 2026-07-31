"""Shared Google OAuth2 token refresh helper for all Google tools."""

import hashlib
import os
import time
from logging import getLogger

import httpx

from apowerb.configs.settings import get_settings
from apowerb.tools_store.portfolio.integration_status import (
    INTEGRATION_ERROR,
    INTEGRATION_EXPIRED,
    INTEGRATION_MISSING,
    IntegrationStatusError,
)

logger = getLogger(__name__)

_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_token_cache: dict[str, dict] = {}
_CACHE_TTL_SECONDS = 50 * 60  # Google tokens last ~60 min

# Per-service lazy-load tracker — value is the invoker the tokens were
# loaded for, so a different invoker triggers a refetch (avoids leaking
# one user's tokens into another's request).
# Keyed by service_env_prefix (e.g. "GOOGLE_GMAIL").
_integration_loaded_for: dict[str, str] = {}

# Map from env-prefix → DB provider key stored in the integrations table.
_ENV_PREFIX_TO_PROVIDER: dict[str, str] = {
    "GOOGLE_DRIVE":    "google_drive",
    "GOOGLE_GMAIL":    "google_gmail",
    "GOOGLE_CALENDAR": "google_calendar",
    "GOOGLE_SHEETS":   "google_sheets",
    "GOOGLE_DOCS":     "google_docs",
}


def _ensure_integration_tokens(service_env_prefix: str) -> None:
    """Lazily load Google integration tokens from DB into env vars.

    Resolves the integration against the **invoker** (user currently
    talking to the agent), not the agent owner. Gmail / Drive / Calendar
    / Sheets / Docs are personal Google services — using AGENT_OWNER
    would leak the agent creator's data to whoever runs the agent.

    Re-fetches when the invoker changes for this service (concurrent
    invocations on the same uvicorn worker).

    Args:
        service_env_prefix: e.g. "GOOGLE_GMAIL", "GOOGLE_DRIVE"
    """
    global _integration_loaded_for

    from apowerb.core.invocation_context import resolve_integration_user
    user = resolve_integration_user(prefer_invoker=True)
    if not user:
        # Not running inside an agent context — skip silently.
        return

    # Already loaded for this user — skip
    if _integration_loaded_for.get(service_env_prefix) == user:
        return

    # Different user or first load — clear stale env var and refetch
    env_key = f"{service_env_prefix}_REFRESH_TOKEN"
    if _integration_loaded_for.get(service_env_prefix):
        os.environ.pop(env_key, None)
        logger.info(
            "Google %s invoker changed (%s → %s) — clearing cached tokens",
            service_env_prefix,
            _integration_loaded_for.get(service_env_prefix),
            user,
        )
        # Drop the latch with the env var — leaving the previous invoker
        # latched while their token is popped would strand them on
        # INTEGRATION_MISSING if this invoker's load fails (their next
        # call would early-return without refetching).
        _integration_loaded_for.pop(service_env_prefix, None)

    provider = _ENV_PREFIX_TO_PROVIDER.get(service_env_prefix)
    if not provider:
        logger.warning("No DB provider mapping for env prefix %s", service_env_prefix)
        _integration_loaded_for[service_env_prefix] = user
        return

    try:
        from apowerb.integrations.helpers import fetch_integration_configs
        configs = fetch_integration_configs(provider, user=user)
        refresh_token = configs.get("refresh_token")
        if refresh_token:
            os.environ[env_key] = refresh_token
            # Latch only on success — caching a failed load would skip the
            # DB refetch forever, so a user who connects AFTER a failed tool
            # call would stay INTEGRATION_MISSING until the next restart.
            _integration_loaded_for[service_env_prefix] = user
            logger.info(
                "Google integration tokens loaded for provider=%s invoker=%s",
                provider, user,
            )
        else:
            logger.warning(
                "Google integration found but refresh_token is empty for provider=%s invoker=%s",
                provider, user,
            )
    except Exception as e:
        logger.warning(
            "Could not load Google integration tokens for provider=%s invoker=%s: %s",
            provider, user, e,
        )


def get_google_access_token(service_env_prefix: str) -> str:
    """Exchange a stored Google refresh token for a fresh access token.

    Args:
        service_env_prefix: Environment variable prefix, e.g. "GOOGLE_GMAIL".
            Expects {prefix}_REFRESH_TOKEN to be set.

    Returns:
        A valid Google access token.

    Raises:
        RuntimeError: If credentials are missing or the refresh fails.
    """
    # Lazily reload from DB if the env var was cleared (e.g. after a reconnect).
    _ensure_integration_tokens(service_env_prefix)

    refresh_token = os.getenv(f"{service_env_prefix}_REFRESH_TOKEN")
    settings = get_settings()
    client_id = settings.google_integration_client_id
    client_secret = settings.google_integration_client_secret

    if not refresh_token or not client_id or not client_secret:
        raise IntegrationStatusError(
            code=INTEGRATION_MISSING,
            provider=_ENV_PREFIX_TO_PROVIDER.get(service_env_prefix, service_env_prefix.lower()),
            message=(
                f"Google credentials are not configured for {service_env_prefix}. "
                "The user must connect their Google account first via the Integrations page."
            ),
        )

    cache_key = hashlib.sha256(f"{client_id}:{refresh_token}".encode()).hexdigest()
    now = time.time()
    cached = _token_cache.get(cache_key)
    if cached and now < cached["expires_at"]:
        return cached["access_token"]

    resp = httpx.post(
        _GOOGLE_TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )

    if resp.status_code != 200:
        body = resp.text
        logger.error(
            "Google token refresh failed for %s: %s - %s",
            service_env_prefix,
            resp.status_code,
            body,
        )
        provider = _ENV_PREFIX_TO_PROVIDER.get(service_env_prefix, service_env_prefix.lower())
        if "invalid_grant" in body.lower():
            raise IntegrationStatusError(
                code=INTEGRATION_EXPIRED,
                provider=provider,
                message=(
                    f"The Google refresh token for {service_env_prefix} has expired or been revoked. "
                    "The user must reconnect their Google account."
                ),
            )
        raise IntegrationStatusError(
            code=INTEGRATION_ERROR,
            provider=provider,
            message=(
                f"Failed to refresh Google access token for {service_env_prefix} (HTTP {resp.status_code}). "
                "This is not a connect/reconnect issue."
            ),
        )

    data = resp.json()
    access_token = data.get("access_token")
    if not access_token:
        raise RuntimeError("Google token endpoint did not return an access_token.")

    _token_cache[cache_key] = {
        "access_token": access_token,
        "expires_at": now + _CACHE_TTL_SECONDS,
    }

    return access_token


def google_auth_headers(service_env_prefix: str) -> dict[str, str]:
    """Return Authorization header dict for Google API calls."""
    return {"Authorization": f"Bearer {get_google_access_token(service_env_prefix)}"}

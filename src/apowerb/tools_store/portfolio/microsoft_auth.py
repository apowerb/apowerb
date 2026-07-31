"""Shared Microsoft OAuth2 token refresh helper for all Microsoft Graph tools.

Mirrors the structure of ``google_auth.py`` but targets the Microsoft
identity platform (Azure AD v2.0).  Each Microsoft service (Teams,
OneDrive, Outlook) calls ``get_microsoft_access_token()`` with its own
environment-variable prefix and OAuth scope, and receives a cached
access token in return.

Typical usage inside a tool module::

    from apowerb.tools_store.portfolio.microsoft_auth import microsoft_auth_headers

    headers = microsoft_auth_headers("TEAMS", scope="offline_access Chat.Read")
"""

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

_CACHE_TTL_SECONDS = 50 * 60  # Microsoft tokens last ~60 min; refresh at 50

# Access-token cache keyed by ``(service_env_prefix, invoker)``. Being keyed by
# the invoker's identity, distinct users never share an entry (no cross-tenant
# leak), yet a warm call costs nothing — it is checked BEFORE any DB read, so we
# don't hammer the shared Postgres per Graph call. Storing the freshly-exchanged
# access token here on rotation also means a transient DB-persist hiccup can't
# strand the process on a dead refresh token for the TTL window.
#
# NOTE: this module deliberately holds NO *shared* per-invoker token state.
# The refresh token is resolved into a LOCAL value on every miss (see
# ``_resolve_refresh_token``). Stashing a resolved refresh token in
# ``os.environ`` — as this module used to — races across concurrent invocations
# on the single uvicorn worker and leaks one user's mailbox into another's send
# (incident 2026-07-03).
_token_cache: dict[tuple[str, str], dict] = {}


def _invoker_cache_key(service_env_prefix: str) -> tuple[str, str]:
    """Cache key scoping the access token to the current invoker.

    Uses the resolved invoker identity (falls back to ``AGENT_OWNER`` / "" for
    background runs) so two users on the same worker never share an entry.
    """
    from apowerb.core.invocation_context import resolve_integration_user
    return (service_env_prefix, resolve_integration_user(prefer_invoker=True) or "")


def clear_integration_cache() -> None:
    """Drop all cached access tokens. Called on integration connect/disconnect
    so the next call re-resolves fresh tokens from the DB."""
    _token_cache.clear()


# Map from env-prefix → DB provider key stored in the integrations table.
_ENV_PREFIX_TO_PROVIDER: dict[str, str] = {
    "OUTLOOK":    "microsoft_outlook",
    "ONEDRIVE":   "microsoft_onedrive",
    "TEAMS":      "microsoft_teams",
    "SHAREPOINT": "microsoft_sharepoint",
}

# Map from env-prefix → the env var name that holds the refresh token.
_ENV_PREFIX_TO_REFRESH_KEY: dict[str, str] = {
    "OUTLOOK":    "OUTLOOK_REFRESH_TOKEN",
    "ONEDRIVE":   "ONEDRIVE_REFRESH_TOKEN",
    "TEAMS":      "TEAMS_REFRESH_TOKEN",
    "SHAREPOINT": "SHAREPOINT_REFRESH_TOKEN",
}


# Services whose token is ALWAYS the invoker's own personal resource, resolved
# from the DB and never trusted from a process-global env var — a concurrent
# caller (e.g. a mass campaign under ``env_scope``) could poison that global and
# make us cross tenants (incident 2026-07-03). Other Microsoft services (Teams)
# stay env-first for backward-compat with their own owner-scoped loaders.
_SELF_RESOLVE_PREFIXES = frozenset({"OUTLOOK"})


def _db_refresh_token(service_env_prefix: str) -> str | None:
    """Fetch the refresh token from the invoker's DB integration row.

    :func:`fetch_integration_configs` captures the invoker ``ContextVar`` up
    front (before its internal thread hop), so the token belongs to the user
    actually running the agent. Returns ``None`` on any miss/error.
    """
    provider = _ENV_PREFIX_TO_PROVIDER.get(service_env_prefix)
    if not provider:
        return None
    try:
        from apowerb.core.invocation_context import resolve_integration_user
        from apowerb.integrations.helpers import fetch_integration_configs
        configs = fetch_integration_configs(provider)
        refresh_token = configs.get("refresh_token")
        if refresh_token:
            logger.info(
                "Microsoft integration token resolved for provider=%s invoker=%s",
                provider, resolve_integration_user(prefer_invoker=True),
            )
            return refresh_token
        logger.warning(
            "Microsoft integration found but refresh_token is empty for provider=%s",
            provider,
        )
    except Exception as e:
        logger.warning(
            "Could not resolve Microsoft integration token for provider=%s: %s",
            provider, e,
        )
    return None


def _resolve_refresh_token(service_env_prefix: str) -> str | None:
    """Resolve the refresh token for this invocation as a LOCAL value.

    The value is RETURNED, never written back to ``os.environ`` — a
    process-global refresh token races across concurrent invocations on the
    single uvicorn worker and sends from the wrong mailbox (incident
    2026-07-03; same class as review-security C6).

    Strategy depends on the service:

    * ``OUTLOOK`` (``_SELF_RESOLVE_PREFIXES``): the mailbox is always the
      invoker's own, so resolve from the DB integration row FIRST. A
      ``{prefix}_REFRESH_TOKEN`` env var is consulted only as a last resort
      (legacy single-account deploys). We deliberately do NOT trust the env var
      over the DB, so a concurrent caller that poisons the global (a mass
      campaign under ``env_scope``) can never make us send from their mailbox.
    * Other services (``TEAMS`` …): resolved env-first, because their own
      owner-scoped loaders / ``env_scope`` callers set the env var deliberately
      and expect us to honour it. Their pre-existing behaviour is untouched.
    """
    env_key = _ENV_PREFIX_TO_REFRESH_KEY.get(
        service_env_prefix, f"{service_env_prefix}_REFRESH_TOKEN"
    )
    if service_env_prefix in _SELF_RESOLVE_PREFIXES:
        return _db_refresh_token(service_env_prefix) or os.getenv(env_key)
    # Env-first for the remaining services (caller sets it deliberately).
    return os.getenv(env_key) or _db_refresh_token(service_env_prefix)


def _build_token_url(service_env_prefix: str) -> str:
    """Build the Microsoft token endpoint URL.

    Checks ``{prefix}_TENANT_ID`` env var first, then falls back to the
    application-wide ``microsoft_integration_tenant_id`` setting.
    """
    tenant = (
        os.getenv(f"{service_env_prefix}_TENANT_ID")
        or get_settings().microsoft_integration_tenant_id
    )
    return f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"


def get_microsoft_access_token(
    service_env_prefix: str,
    *,
    scope: str = "offline_access",
    service_label: str | None = None,
) -> str:
    """Exchange a stored Microsoft refresh token for a fresh access token.

    Args:
        service_env_prefix: Environment variable prefix, e.g. ``"TEAMS"``.
            Expects ``{prefix}_REFRESH_TOKEN`` to be set.  Optionally reads
            ``{prefix}_CLIENT_ID``, ``{prefix}_CLIENT_SECRET``, and
            ``{prefix}_TENANT_ID`` from environment, falling back to
            application settings.
        scope: OAuth2 scope string sent in the token request.
            Each service should pass its own required scopes
            (e.g. ``"offline_access Mail.Read"``).
        service_label: Human-readable name used in error messages
            (e.g. ``"Outlook Mail"``).  Defaults to *service_env_prefix*.

    Returns:
        A valid Microsoft Graph access token.

    Raises:
        RuntimeError: If credentials are missing or the refresh fails.
    """
    label = service_label or service_env_prefix
    settings = get_settings()

    # ---- warm-path cache check (invoker-scoped, BEFORE any DB read) ----
    cache_key = _invoker_cache_key(service_env_prefix)
    now = time.time()
    cached = _token_cache.get(cache_key)
    if cached and now < cached["expires_at"]:
        return cached["access_token"]

    # Resolve the refresh token for THIS invocation as a local value —
    # never via a process-global env var (races across concurrent
    # invocations; incident 2026-07-03).
    refresh_token = _resolve_refresh_token(service_env_prefix)
    client_id = (
        os.getenv(f"{service_env_prefix}_CLIENT_ID")
        or settings.microsoft_integration_client_id
    )
    client_secret = (
        os.getenv(f"{service_env_prefix}_CLIENT_SECRET")
        or settings.microsoft_integration_client_secret
    )

    if not refresh_token or not client_id or not client_secret:
        raise IntegrationStatusError(
            code=INTEGRATION_MISSING,
            provider=_ENV_PREFIX_TO_PROVIDER.get(service_env_prefix, service_env_prefix.lower()),
            message=(
                f"{label} credentials are not configured. "
                f"The user must connect their {label} account first via the Integrations page."
            ),
        )

    # ---- token refresh ----
    token_url = _build_token_url(service_env_prefix)

    resp = httpx.post(
        token_url,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
            "scope": scope,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )

    if resp.status_code != 200:
        body = resp.text
        logger.error(
            "%s token refresh failed: %s - %s",
            label,
            resp.status_code,
            body,
        )
        provider = _ENV_PREFIX_TO_PROVIDER.get(service_env_prefix, service_env_prefix.lower())
        if "invalid_grant" in body.lower():
            raise IntegrationStatusError(
                code=INTEGRATION_EXPIRED,
                provider=provider,
                message=(
                    f"The {label} refresh token has expired or been revoked. "
                    f"The user must reconnect their {label} account."
                ),
            )
        raise IntegrationStatusError(
            code=INTEGRATION_ERROR,
            provider=provider,
            message=(
                f"Failed to refresh {label} access token (HTTP {resp.status_code}). "
                "This is not a connect/reconnect issue."
            ),
        )

    data = resp.json()
    access_token = data.get("access_token")
    if not access_token:
        raise RuntimeError("Microsoft token endpoint did not return an access_token.")

    # Microsoft rotates the refresh_token on every exchange — if we don't
    # persist the new one, the next refresh will use a stale refresh_token
    # and fail with invalid_grant ("my Outlook keeps expiring"). We persist
    # it to the invoker's DB row (durable, per-user); the next call re-resolves
    # it from there.
    new_refresh_token = data.get("refresh_token")
    new_scope = data.get("scope")
    provider = _ENV_PREFIX_TO_PROVIDER.get(service_env_prefix)
    if new_refresh_token:
        env_key = _ENV_PREFIX_TO_REFRESH_KEY.get(
            service_env_prefix, f"{service_env_prefix}_REFRESH_TOKEN"
        )
        # Only keep an env-based caller's token fresh (env_scope / teams /
        # onedrive_core, which set the env var deliberately). For
        # self-resolving callers (Outlook) the env var is unset — we leave
        # it unset so concurrent invocations never share a process-global
        # token (incident 2026-07-03). ``setdefault``-style guard: touch the
        # env var only if it already exists.
        if os.getenv(env_key) is not None:
            os.environ[env_key] = new_refresh_token
    if provider and (new_refresh_token or access_token):
        try:
            from apowerb.integrations.helpers import persist_refreshed_tokens

            persist_refreshed_tokens(
                provider,
                access_token=access_token,
                refresh_token=new_refresh_token,
                scope=new_scope,
            )
        except Exception as exc:
            # ERROR, not warning: if this is the only durable write of the
            # rotated token and it failed, the DB now holds a token Microsoft
            # has already invalidated. The invoker cache below keeps this
            # process alive for the TTL window, but the row needs attention.
            logger.error(
                "%s: persist of rotated refresh_token FAILED — DB may hold a "
                "stale token (reconnect may be required): %s",
                label,
                exc,
            )

    # Cache the freshly-exchanged access token under the invoker key so the
    # next call on this process is a warm hit (no DB, no HTTP) — and so a
    # failed persist above cannot immediately strand us on a dead token.
    _token_cache[cache_key] = {
        "access_token": access_token,
        "expires_at": now + _CACHE_TTL_SECONDS,
    }

    return access_token


def microsoft_auth_headers(
    service_env_prefix: str,
    *,
    scope: str = "offline_access",
    service_label: str | None = None,
) -> dict[str, str]:
    """Return Authorization header dict for Microsoft Graph API calls.

    Accepts the same arguments as ``get_microsoft_access_token`` and
    returns a dict ready to be passed as ``headers=`` to *httpx* calls.
    """
    token = get_microsoft_access_token(
        service_env_prefix,
        scope=scope,
        service_label=service_label,
    )
    return {"Authorization": f"Bearer {token}"}

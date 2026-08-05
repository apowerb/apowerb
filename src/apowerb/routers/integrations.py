import secrets
from logging import getLogger

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel, EmailStr

from apowerb.helpers.database import get_db
from apowerb.auth.dependencies import get_current_user
from apowerb.helpers import oauth_state_store
from apowerb.models import User, Integration
from apowerb.integrations.github import GitHubIntegrationService
from apowerb.integrations.microsoft import (
    MicrosoftIntegrationService,
    SUPPORTED_MICROSOFT_SERVICES,
    IntegrationTokenExpiredError,
    is_access_token_expired,
)
from apowerb.integrations.google import GoogleIntegrationService, GOOGLE_SERVICES
from apowerb.integrations import odoo as odoo_integration

logger = getLogger(__name__)

router = APIRouter(prefix="/integrations", tags=["integrations"])


# ---------------------------------------------------------------------------
# OAuth provider keys used when storing / validating the CSRF state.
# They MUST match what the callback validates — treat these as constants.
# ---------------------------------------------------------------------------
_PROVIDER_GITHUB = "github"


def _microsoft_provider(service: str) -> str:
    return f"microsoft_{service}"


# ---------------------------------------------------------------------------
# Microsoft reset helpers
# ---------------------------------------------------------------------------

def _reset_outlook_module_state() -> None:
    """Clear all in-memory token state so the next tool call re-fetches tokens
    from the DB. Called on both disconnect and reconnect."""
    try:
        import os
        # Invoker-scoped access-token cache lives in microsoft_auth.
        import apowerb.tools_store.portfolio.microsoft_auth as ma
        ma.clear_integration_cache()
        # Short-lived shared-mailbox cache lives in outlook_mail.
        import apowerb.tools_store.portfolio.outlook_mail as om
        om.reset_shared_mailbox_cache()
        os.environ.pop("OUTLOOK_REFRESH_TOKEN", None)
    except Exception:
        pass  # Never block the main action


def _reset_onedrive_module_state() -> None:
    """Clear all in-memory token state in onedrive so the next tool call
    re-fetches tokens from the DB. Called on both disconnect and reconnect."""
    try:
        import os
        import apowerb.tools_store.portfolio.onedrive as od
        od._integration_loaded = False
        import apowerb.tools_store.portfolio.microsoft_auth as ma
        ma._token_cache.clear()
        os.environ.pop("ONEDRIVE_REFRESH_TOKEN", None)
    except Exception:
        pass  # Never block the main action


# ---------------------------------------------------------------------------
# Google reset helpers
# ---------------------------------------------------------------------------

def _clear_google_token_cache() -> None:
    """Clear the shared google_auth in-memory token cache AND lazy-load flags.

    All Google tool modules share one cache keyed by hash(client_id, refresh_token).
    Clearing both ensures the next tool call re-fetches the fresh token from DB
    instead of skipping the lazy-reload because the flag was already True.
    """
    try:
        import apowerb.tools_store.portfolio.google_auth as ga
        ga._token_cache.clear()
        ga._integration_loaded_for.clear()  # Forces lazy-reload on next tool call
    except Exception:
        logger.exception("Failed to clear google_auth in-memory token state")


def _reset_google_drive_module_state() -> None:
    try:
        import os
        _clear_google_token_cache()
        os.environ.pop("GOOGLE_DRIVE_REFRESH_TOKEN", None)
    except Exception:
        pass


def _reset_google_gmail_module_state() -> None:
    try:
        import os
        _clear_google_token_cache()
        os.environ.pop("GOOGLE_GMAIL_REFRESH_TOKEN", None)
    except Exception:
        pass


def _reset_google_calendar_module_state() -> None:
    try:
        import os
        _clear_google_token_cache()
        os.environ.pop("GOOGLE_CALENDAR_REFRESH_TOKEN", None)
    except Exception:
        pass


def _reset_google_sheets_module_state() -> None:
    try:
        import os
        _clear_google_token_cache()
        os.environ.pop("GOOGLE_SHEETS_REFRESH_TOKEN", None)
    except Exception:
        pass


def _reset_google_docs_module_state() -> None:
    try:
        import os
        _clear_google_token_cache()
        os.environ.pop("GOOGLE_DOCS_REFRESH_TOKEN", None)
    except Exception:
        pass


# Single dispatch map — add a new Google service here and nowhere else.
_GOOGLE_RESET_HANDLERS: dict = {
    "google_drive":    _reset_google_drive_module_state,
    "google_gmail":    _reset_google_gmail_module_state,
    "google_calendar": _reset_google_calendar_module_state,
    "google_sheets":   _reset_google_sheets_module_state,
    "google_docs":     _reset_google_docs_module_state,
}


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _validate_microsoft_service(service: str) -> str:
    """Validate the Microsoft service key; raises HTTP 400 for unknown values."""
    service = service.lower()
    if service not in SUPPORTED_MICROSOFT_SERVICES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Unknown Microsoft service '{service}'. "
                f"Supported: {', '.join(SUPPORTED_MICROSOFT_SERVICES)}."
            ),
        )
    return service


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------

class GitHubCallbackRequest(BaseModel):
    """Payload the frontend POSTs after GitHub redirects back."""
    code: str
    state: str

class MicrosoftCallbackRequest(BaseModel):
    """Payload the frontend POSTs after Microsoft redirects back."""
    code: str
    state: str
    service: str = "outlook"  # "outlook", "teams", "onedrive", "sharepoint"
    redirect_uri: str | None = None

class GoogleCallbackRequest(BaseModel):
    """Payload the frontend POSTs after Google redirects back."""
    code: str
    state: str
    service: str  # "google_drive", "google_gmail", "google_calendar", etc.
    redirect_uri: str | None = None


class SharedMailboxRequest(BaseModel):
    email: EmailStr


class OdooConnectRequest(BaseModel):
    """Payload the frontend POSTs to connect an Odoo SaaS instance."""
    url:      str
    database: str
    login:    str
    api_key:  str


# ---------------------------------------------------------------------------
# GitHub endpoints
# ---------------------------------------------------------------------------

@router.get("/github/connect")
async def github_connect(
    redirect_uri: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Generate the GitHub OAuth authorisation URL for the authenticated user.

    The ``state`` parameter is persisted server-side (``oauth_states`` table)
    so the callback can validate it and prevent CSRF-based binding of an
    attacker-controlled GitHub account onto the authenticated user.
    """
    state = await oauth_state_store.create(
        db=db, user_id=current_user.user_id, provider=_PROVIDER_GITHUB
    )
    oauth_url = GitHubIntegrationService.get_oauth_url(state, redirect_uri=redirect_uri)
    return {"url": oauth_url, "state": state}


@router.post("/github/callback")
async def github_callback(
    payload: GitHubCallbackRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Complete the GitHub OAuth flow.

    The frontend calls this after GitHub redirects back to the callback page
    with a temporary `code`. This endpoint:
      1. Validates the CSRF ``state`` against the server-side store
      2. Exchanges the code for an access token
      3. Fetches the GitHub user profile
      4. Upserts the integration row for the current user
      5. Returns basic integration info (never the raw token)
    """
    await oauth_state_store.consume(
        db=db,
        state=payload.state,
        user_id=current_user.user_id,
        provider=_PROVIDER_GITHUB,
    )

    token_data = await GitHubIntegrationService.exchange_code_for_token(payload.code)
    if not token_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to exchange GitHub authorisation code for token. "
                   "The code may have expired — please try connecting again.",
        )

    access_token: str = token_data.get("access_token", "")
    scopes: str = token_data.get("scope", "")

    if not access_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="GitHub did not return an access token.",
        )

    github_user = await GitHubIntegrationService.get_github_user(access_token)
    if not github_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to fetch GitHub user profile.",
        )

    integration = await GitHubIntegrationService.save_integration(
        db=db,
        user_id=current_user.user_id,
        github_data=github_user,
        access_token=access_token,
        scopes=scopes,
    )

    return {
        "success": True,
        "provider": "github",
        "username": integration.provider_username,
        "scopes": integration.scopes,
        "meta": integration.meta,
    }


# ---------------------------------------------------------------------------
# Microsoft endpoints
# ---------------------------------------------------------------------------

@router.get("/microsoft/{service}/connect")
async def microsoft_service_connect(
    service: str,
    redirect_uri: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Generate the Microsoft OAuth authorisation URL for a specific service.

    Args:
        service: One of 'outlook', 'teams', 'sharepoint', 'onedrive'.
                 Determines which Graph API scopes are requested.
    """
    service = _validate_microsoft_service(service)
    state = await oauth_state_store.create(
        db=db,
        user_id=current_user.user_id,
        provider=_microsoft_provider(service),
    )
    oauth_url = MicrosoftIntegrationService.get_oauth_url(
        state, service=service, redirect_uri=redirect_uri
    )
    return {"url": oauth_url, "state": state, "service": service}


@router.post("/microsoft/{service}/callback")
async def microsoft_service_callback(
    service: str,
    payload: MicrosoftCallbackRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Complete the Microsoft OAuth flow for a specific service.

    The frontend calls this after Microsoft redirects back to the callback page
    with a temporary ``code``. This endpoint:
      1. Validates the service key
      2. Validates the CSRF ``state`` against the server-side store
      3. Exchanges the code for an access + refresh token (service-specific scopes)
      4. Fetches the Microsoft Graph user profile
      5. Upserts the integration row under provider='microsoft_{service}'
      6. Returns basic integration info (never the raw token)
    """
    service = _validate_microsoft_service(service)

    await oauth_state_store.consume(
        db=db,
        state=payload.state,
        user_id=current_user.user_id,
        provider=_microsoft_provider(service),
    )

    token_data = await MicrosoftIntegrationService.exchange_code_for_token(
        payload.code, service=service, redirect_uri=payload.redirect_uri
    )
    if not token_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Failed to exchange Microsoft authorisation code for token "
                f"(service={service}). The code may have expired — please try connecting again."
            ),
        )

    access_token: str  = token_data.get("access_token", "")
    refresh_token: str = token_data.get("refresh_token", "")
    scopes: str        = token_data.get("scope", "")

    if not access_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Microsoft did not return an access token.",
        )

    ms_user = await MicrosoftIntegrationService.get_microsoft_user(access_token)
    if not ms_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to fetch Microsoft user profile.",
        )

    integration = await MicrosoftIntegrationService.save_integration(
        db=db,
        user_id=current_user.user_id,
        ms_data=ms_user,
        access_token=access_token,
        refresh_token=refresh_token,
        scopes=scopes,
        service=service,
    )

    # Reset in-memory state for the relevant Microsoft service module.
    if service == "outlook":
        _reset_outlook_module_state()
    elif service == "onedrive":
        _reset_onedrive_module_state()

    return {
        "success":  True,
        "provider": f"microsoft_{service}",
        "service":  service,
        "email":    integration.provider_username,
        "username": integration.provider_username,
        "scopes":   integration.scopes,
        "meta":     integration.meta,
    }


# ---------------------------------------------------------------------------
# Microsoft Outlook — shared mailboxes
# ---------------------------------------------------------------------------

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"


async def _get_outlook_integration(
    db: AsyncSession, user_id: int,
) -> Integration:
    result = await db.execute(
        select(Integration).where(
            Integration.user_id == user_id,
            Integration.provider == "microsoft_outlook",
        )
    )
    integration = result.scalar_one_or_none()
    if not integration:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No Outlook integration found. Connect your Microsoft account first.",
        )
    return integration


@router.get("/microsoft/outlook/shared-mailboxes")
async def list_shared_mailboxes(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """Return the shared mailboxes currently configured for this user."""
    integration = await _get_outlook_integration(db, current_user.user_id)
    meta = integration.meta or {}
    shared = [m for m in (meta.get("shared_mailboxes") or []) if isinstance(m, str)]
    active = meta.get("active_shared_mailbox")
    if active and active not in shared:
        active = None
    return {"mailboxes": shared, "active": active}


@router.get("/microsoft/outlook/debug/token-scopes")
async def debug_outlook_token_scopes(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """Decode the stored access token and return its scopes (debug).

    Auto-refreshes if the token is expired.
    """
    import base64, json
    try:
        access_token = await MicrosoftIntegrationService.get_valid_access_token(
            db, current_user.user_id, service="outlook",
        )
    except IntegrationTokenExpiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Outlook connection expired: {exc}. Please reconnect.",
        )
    try:
        payload_b64 = access_token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        logger.exception("[INTEGRATIONS] Failed to decode Outlook access token for debug endpoint")
        return {"error": "Failed to decode access token"}
    return {
        "scp": claims.get("scp"),
        "roles": claims.get("roles"),
        "aud": claims.get("aud"),
        "upn": claims.get("upn") or claims.get("preferred_username"),
        "tid": claims.get("tid"),
        "exp": claims.get("exp"),
    }


@router.get("/microsoft/outlook/shared-mailboxes/suggestions")
async def suggest_shared_mailboxes(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """Best-effort discovery of shared mailboxes via /me/memberOf.

    Microsoft Graph doesn't expose the list of mailboxes a user has delegate
    access to without admin scopes. As a fallback, we list the user's
    Microsoft 365 groups — they are mail-enabled and often represent shared
    team mailboxes. The user can still add any address manually.
    """
    try:
        access_token = await MicrosoftIntegrationService.get_valid_access_token(
            db, current_user.user_id, service="outlook",
        )
    except IntegrationTokenExpiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Outlook connection expired: {exc}. Please reconnect.",
        )

    suggestions: list[dict] = []
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{_GRAPH_BASE}/me/memberOf",
                headers={"Authorization": f"Bearer {access_token}"},
                params={"$select": "mail,displayName,groupTypes,mailEnabled", "$top": "100"},
                timeout=10.0,
            )
        if resp.status_code == 200:
            for item in resp.json().get("value", []):
                mail = item.get("mail")
                if mail and item.get("mailEnabled"):
                    suggestions.append({"email": mail, "name": item.get("displayName") or mail})
    except Exception as exc:
        logger.warning("[outlook] suggestions fetch failed: %s", exc)

    return {"suggestions": suggestions}


@router.put("/microsoft/outlook/shared-mailboxes/active")
async def set_active_shared_mailbox(
    payload: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """Mark one of the configured shared mailboxes as the active default."""
    integration = await _get_outlook_integration(db, current_user.user_id)
    meta = integration.meta or {}
    shared = [m for m in (meta.get("shared_mailboxes") or []) if isinstance(m, str)]

    email = payload.get("email")
    if email is not None and email not in shared:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"'{email}' is not in your shared mailboxes. Add it first.",
        )

    meta["active_shared_mailbox"] = email
    integration.meta = meta  # type: ignore[assignment]
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(integration, "meta")
    await db.commit()
    return {"success": True, "active": email}


@router.post("/microsoft/outlook/shared-mailboxes")
async def add_shared_mailbox(
    payload: SharedMailboxRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    integration = await _get_outlook_integration(db, current_user.user_id)

    try:
        access_token = await MicrosoftIntegrationService.get_valid_access_token(
            db, current_user.user_id, service="outlook",
        )
    except IntegrationTokenExpiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Outlook connection expired: {exc}. Please reconnect.",
        )

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{_GRAPH_BASE}/users/{payload.email}/mailFolders",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"$top": "1"},
            timeout=15.0,
        )

    if resp.status_code >= 400:
        logger.warning(
            "Shared mailbox access check failed for %s: HTTP %s — %s",
            payload.email, resp.status_code, resp.text[:300],
        )
        if resp.status_code == 401:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Outlook authentication failed. Please reconnect your account.",
            )
        if resp.status_code == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Mailbox '{payload.email}' does not exist in the tenant.",
            )
        if resp.status_code == 403:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"No delegate access to '{payload.email}'. "
                       "Ask your Microsoft 365 admin to grant you Full Access.",
            )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Microsoft Graph error (HTTP {resp.status_code}) while validating mailbox access.",
        )

    meta = integration.meta or {}
    shared: list[str] = meta.get("shared_mailboxes", [])
    if payload.email not in shared:
        shared.append(payload.email)
    meta["shared_mailboxes"] = shared
    integration.meta = meta  # type: ignore[assignment]

    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(integration, "meta")
    await db.commit()
    await db.refresh(integration)

    return {"success": True, "shared_mailboxes": meta["shared_mailboxes"]}


@router.delete("/microsoft/outlook/shared-mailboxes/{email}")
async def remove_shared_mailbox(
    email: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    integration = await _get_outlook_integration(db, current_user.user_id)

    meta = integration.meta or {}
    shared: list[str] = meta.get("shared_mailboxes", [])
    if email not in shared:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"'{email}' is not in your shared mailboxes list.",
        )

    shared.remove(email)
    meta["shared_mailboxes"] = shared
    integration.meta = meta  # type: ignore[assignment]

    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(integration, "meta")
    await db.commit()
    await db.refresh(integration)

    return {"success": True, "shared_mailboxes": meta["shared_mailboxes"]}


# ---------------------------------------------------------------------------
# Google endpoints
# ---------------------------------------------------------------------------

@router.get("/google/connect")
async def google_connect(
    service: str,
    redirect_uri: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Generate the Google OAuth authorisation URL for a specific Google service.

    Query params:
        service: One of "google_drive", "google_gmail", "google_calendar",
                 "google_sheets", "google_docs".
        redirect_uri: Optional override for the callback URL.
    """
    if service not in GOOGLE_SERVICES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown Google service '{service}'. "
                   f"Valid values: {', '.join(GOOGLE_SERVICES.keys())}",
        )

    state = await oauth_state_store.create(
        db=db, user_id=current_user.user_id, provider=service
    )
    oauth_url = GoogleIntegrationService.get_oauth_url(
        service=service, state=state, redirect_uri=redirect_uri,
    )
    return {"url": oauth_url, "state": state}


@router.post("/google/callback")
async def google_callback(
    payload: GoogleCallbackRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Complete the Google OAuth flow for a specific Google service.

    The frontend calls this after Google redirects back to the callback page
    with a temporary ``code``. This endpoint:
      1. Validates the requested service
      2. Validates the CSRF ``state`` against the server-side store
      3. Exchanges the code for access + refresh tokens
      4. Fetches the Google user profile
      5. Upserts the integration row with provider=service
      6. Resets the in-memory token state for that service module
      7. Returns basic integration info (never the raw token)
    """
    service = payload.service
    if service not in GOOGLE_SERVICES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown Google service '{service}'. "
                   f"Valid values: {', '.join(GOOGLE_SERVICES.keys())}",
        )

    await oauth_state_store.consume(
        db=db,
        state=payload.state,
        user_id=current_user.user_id,
        provider=service,
    )

    token_data = await GoogleIntegrationService.exchange_code_for_token(
        payload.code, redirect_uri=payload.redirect_uri,
    )
    if not token_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to exchange Google authorisation code for token. "
                   "The code may have expired — please try connecting again.",
        )

    access_token: str = token_data.get("access_token", "")
    refresh_token: str = token_data.get("refresh_token", "")
    scopes: str = token_data.get("scope", "")

    if not access_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Google did not return an access token.",
        )

    google_user = await GoogleIntegrationService.get_google_user(access_token)
    if not google_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to fetch Google user profile.",
        )

    integration = await GoogleIntegrationService.save_integration(
        db=db,
        user_id=current_user.user_id,
        google_data=google_user,
        access_token=access_token,
        refresh_token=refresh_token,
        scopes=scopes,
        service=service,
    )

    # Reset in-memory token state for the connected service so the next tool
    # call picks up the freshly stored refresh token instead of a stale one.
    reset_fn = _GOOGLE_RESET_HANDLERS.get(service)
    if reset_fn:
        reset_fn()

    return {
        "success": True,
        "provider": service,
        "email": integration.provider_username,
        "username": integration.provider_username,
        "scopes": integration.scopes,
        "meta": integration.meta,
    }


# ---------------------------------------------------------------------------
# Odoo endpoints (non-OAuth: API key + URL + database)
# ---------------------------------------------------------------------------

@router.post("/odoo/connect")
async def odoo_connect(
    payload: OdooConnectRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Authenticate against the user-provided Odoo instance and persist
    the credentials (api_key is encrypted at rest).

    On success the integration row is upserted under provider='odoo'.
    Returns the same shape as the other integration connect endpoints.
    """
    try:
        uid = await odoo_integration.authenticate(
            payload.url, payload.database, payload.login, payload.api_key,
        )
    except odoo_integration.OdooConnectionError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    integration = await odoo_integration.save_integration(
        db=db,
        user_id=current_user.user_id,
        url=payload.url,
        database=payload.database,
        login=payload.login,
        api_key=payload.api_key,
        uid=uid,
    )

    return {
        "success":  True,
        "provider": odoo_integration.ODOO_PROVIDER,
        "username": integration.provider_username,
        "uid":      uid,
        "meta":     integration.meta,
    }


# ---------------------------------------------------------------------------
# List & disconnect endpoints
# ---------------------------------------------------------------------------

@router.get("/")
async def list_integrations(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return all active integrations for the authenticated user (tokens excluded)."""
    result = await db.execute(
        select(Integration).where(Integration.user_id == current_user.user_id)
    )
    integrations = result.scalars().all()

    from apowerb.helpers.encryptor import decrypt_value

    def _status(integ: Integration) -> str:
        # An integration is active as long as we hold a refresh_token.
        # Microsoft access_tokens expire every ~1h by design — this is NOT a
        # disconnection. The refresh_token (90+ days) is what really controls
        # whether the integration still works. We refresh on demand when an API
        # call needs an access_token; if THAT fails (revoked refresh_token),
        # the user will see the error at call time.
        # Reporting "expired" here just because the cached access_token is
        # past its 1h TTL caused users to see "Reconnect needed" while the
        # integration was still perfectly fine.
        if not (integ.provider or "").startswith("microsoft_"):
            return "active"
        if not integ.refresh_token:
            return "revoked"
        return "active"

    return [
        {
            "id": i.id,
            "provider": i.provider,
            "username": i.provider_username,
            "scopes": i.scopes,
            "connected_at": i.created_at.isoformat() if i.created_at else None,
            "meta": i.meta,
            "token_status": _status(i),
        }
        for i in integrations
    ]


@router.delete("/{provider}", status_code=status.HTTP_200_OK)
async def disconnect_integration(
    provider: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Disconnect (delete) a specific integration for the authenticated user."""
    result = await db.execute(
        select(Integration).where(
            Integration.user_id == current_user.user_id,
            Integration.provider == provider.lower(),
        )
    )
    integration = result.scalar_one_or_none()

    if not integration:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No '{provider}' integration found for this account.",
        )

    await db.delete(integration)
    await db.commit()

    # Reset in-memory token state so the tool stops working immediately.
    provider_lower = provider.lower()

    if provider_lower == "microsoft_outlook":
        _reset_outlook_module_state()
    elif provider_lower == "microsoft_onedrive":
        _reset_onedrive_module_state()
    else:
        # Handles all Google services via the dispatch map.
        reset_fn = _GOOGLE_RESET_HANDLERS.get(provider_lower)
        if reset_fn:
            reset_fn()

    return {"success": True, "message": f"'{provider}' integration disconnected successfully."}

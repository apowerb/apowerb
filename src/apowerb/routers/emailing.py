"""Router for Outlook Mail OAuth flow (tool connection, NOT user login).

Endpoints allow a user to:
1. Get the Microsoft consent URL to grant Mail.Read permission.
2. Exchange the authorization code for tokens and store them as a tool_config.
3. Check whether a connection already exists.
"""

import secrets
import time
from logging import getLogger
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from apowerb.auth.dependencies import get_current_user
from apowerb.configs.settings import get_settings
from apowerb.helpers.emails import get_domain_from_email
from apowerb.schema.tool_config_schema import ToolConfigCreateSchema
from apowerb.tools_store.tools_helpers import (
    register_tool_config,
    tool_config_store,
)
from apowerb.users import schemas as user_schemas

logger = getLogger(__name__)
router = APIRouter()
settings = get_settings()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_OAUTH_SCOPES = "offline_access Mail.Read Mail.Send"
_TOOL_CATEGORY = "outlook_mail"
_TOOL_NAME = "outlook_mail.tool_list_emails"

# The Microsoft app here is the shared `microsoft_integration_*` registration,
# not one of its own: the tokens this flow mints are refreshed later by
# `tools_store/portfolio/microsoft_auth.py` under that same client id, and a
# refresh token issued by a second app would not renew.
#
# The callback itself is `outlook_mail_redirect_uri`, deduced from
# `app_public_url` in `configs/settings.py` along with every other public URL.
# This router used to build it here from `frontend_urls`; deriving one callback
# in a router while the rest are deduced in one place is how two conventions
# start.

# ---------------------------------------------------------------------------
# CSRF protection: module-level store for OAuth state tokens (C2)
# ---------------------------------------------------------------------------
_oauth_states: dict[str, dict] = {}
_OAUTH_STATE_TTL = 600  # 10 minutes


def _cleanup_expired_states() -> None:
    """Remove expired OAuth state tokens to prevent memory leaks."""
    now = time.time()
    expired = [k for k, v in _oauth_states.items() if now >= v["expires"]]
    for k in expired:
        del _oauth_states[k]


def _create_oauth_state(email: str) -> str:
    """Generate a cryptographically random state token and store it."""
    _cleanup_expired_states()
    token = secrets.token_urlsafe(32)
    _oauth_states[token] = {
        "email": email,
        "expires": time.time() + _OAUTH_STATE_TTL,
    }
    return token


def _validate_oauth_state(state: str | None, expected_email: str) -> None:
    """Validate and consume a one-time OAuth state token.

    Raises HTTPException if the state is missing, unknown, expired, or does
    not match the authenticated user's email.
    """
    if not state:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing OAuth state parameter.",
        )

    stored = _oauth_states.pop(state, None)
    if not stored:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or already-used OAuth state token.",
        )

    if time.time() >= stored["expires"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="OAuth state token has expired. Please restart the connection flow.",
        )

    if stored["email"] != expected_email:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="OAuth state does not match the authenticated user.",
        )


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------
class CallbackRequest(BaseModel):
    """Body sent by the frontend after the Microsoft consent redirect."""
    code: str
    state: str | None = None


# ---------------------------------------------------------------------------
# GET /api/emailing/microsoft/auth-url
# ---------------------------------------------------------------------------
@router.get("/emailing/microsoft/auth-url", tags=["emailing"])
async def get_auth_url(
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Generate the Microsoft OAuth consent URL for Outlook Mail access."""
    if not settings.microsoft_integration_client_id:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "Outlook Mail OAuth is not configured "
                "(missing MICROSOFT_INTEGRATION_CLIENT_ID)."
            ),
        )

    tenant = settings.microsoft_integration_tenant_id or "common"
    redirect_uri = settings.outlook_mail_redirect_uri

    # C2: Generate a CSRF-safe state token instead of raw email
    state_token = _create_oauth_state(current_user.email)

    params = urlencode(
        {
            "client_id": settings.microsoft_integration_client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "response_mode": "query",
            "scope": _OAUTH_SCOPES,
            "state": state_token,
        }
    )
    auth_url = (
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize?{params}"
    )
    return {"auth_url": auth_url}


# ---------------------------------------------------------------------------
# POST /api/emailing/microsoft/callback
# ---------------------------------------------------------------------------
@router.post("/emailing/microsoft/callback", tags=["emailing"])
async def oauth_callback(
    body: CallbackRequest,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Exchange the authorization code for tokens and persist as tool_config."""
    if (
        not settings.microsoft_integration_client_id
        or not settings.microsoft_integration_client_secret
    ):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Outlook Mail OAuth is not configured on the server.",
        )

    # C2: Validate the CSRF state token
    _validate_oauth_state(body.state, current_user.email)

    tenant = settings.microsoft_integration_tenant_id or "common"
    token_url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
    redirect_uri = settings.outlook_mail_redirect_uri

    # --- Exchange code for tokens -------------------------------------------
    async with httpx.AsyncClient() as client:
        response = await client.post(
            token_url,
            data={
                "client_id": settings.microsoft_integration_client_id,
                "client_secret": settings.microsoft_integration_client_secret,
                "code": body.code,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "scope": _OAUTH_SCOPES,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    if response.status_code != 200:
        logger.warning(
            "Microsoft token exchange failed: %s - %s",
            response.status_code,
            response.text,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Microsoft token exchange failed ({response.status_code}).",
        )

    token_data = response.json()
    refresh_token = token_data.get("refresh_token")
    if not refresh_token:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Microsoft did not return a refresh_token. Ensure 'offline_access' scope is granted.",
        )

    # --- Upsert tool_config -------------------------------------------------
    user_email = current_user.email
    org = get_domain_from_email(user_email)

    # W1: Only store the refresh_token -- client_id and client_secret are
    # read from settings at runtime, never persisted per-tool.
    tool_config = ToolConfigCreateSchema(
        tool_config_name=f"Outlook Mail \u2014 {user_email}",
        tool_name=_TOOL_NAME,
        tool_config_params={
            "OUTLOOK_REFRESH_TOKEN": refresh_token,
        },
        tool_category=_TOOL_CATEGORY,
        owner_id=user_email,
        organization_id=org,
    )

    result = register_tool_config(tool_config=tool_config)

    logger.info(
        "Outlook Mail tool_config created/updated for user=%s, id=%s",
        user_email,
        result.get("tool_config_id"),
    )

    return {"success": True, "tool_config_id": result.get("tool_config_id")}


# ---------------------------------------------------------------------------
# GET /api/emailing/microsoft/status
# ---------------------------------------------------------------------------
@router.get("/emailing/microsoft/status", tags=["emailing"])
async def connection_status(
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Check whether the current user has an active Outlook Mail connection."""
    t = tool_config_store.tool_config_table
    select_query = t.select().where(
        (t.c.tool_category == _TOOL_CATEGORY) & (t.c.owner_id == current_user.email)
    )
    rows = tool_config_store.get_list_tool_configs(select_query)

    if rows:
        row = rows[0]._asdict()
        return {
            "connected": True,
            "tool_config_id": f"tool_config{row['tool_config_id']}",
            "tool_config_name": row.get("tool_config_name"),
        }

    return {"connected": False}

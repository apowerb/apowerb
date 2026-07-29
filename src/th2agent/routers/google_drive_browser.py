from __future__ import annotations

import asyncio
import base64

import httpx
from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from th2agent.auth.dependencies import get_current_user
from th2agent.helpers.database import get_db
from th2agent.helpers.env_scope import env_scope
from th2agent.models import Integration, User

from th2agent.tools_store.portfolio.google_drive import (
    _DRIVE_BASE,
    _EXPORT_MIME_TYPES,
    tool_list_files,
    tool_search_files,
)
from th2agent.tools_store.portfolio.google_auth import google_auth_headers

router = APIRouter(prefix="/api/googledrivebrowser", tags=["google-drive-browser"])

_MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024  # 20 MB hard limit for chat attachments
_SERVICE_PREFIX = "GOOGLE_DRIVE"

# B6 — Serialize access to GOOGLE_DRIVE_REFRESH_TOKEN so two concurrent users
# never observe each other's refresh_token while their sync tool call is
# running. The lock is held for the whole scoped block so the tool always
# reads the env var set for *this* caller.
_gdrive_env_lock = asyncio.Lock()


async def _resolve_google_drive_refresh_token(
    current_user: User,
    db: AsyncSession,
) -> str:
    """Return the refresh_token stored for *current_user*'s Google Drive
    integration, or raise ``RuntimeError`` if missing. No env mutation here —
    callers pass the returned token to :func:`env_scope`.
    """
    result = await db.execute(
        select(Integration).where(
            Integration.user_id == current_user.user_id,
            Integration.provider == "google_drive",
        )
    )
    integration = result.scalar_one_or_none()

    if not integration or not integration.refresh_token:
        raise RuntimeError(
            "Google Drive credentials are not configured. "
            "The user must connect their Google Drive account via the integrations settings page. "
            "Do not retry — inform the user they need to connect Google Drive."
        )

    # B7 — Tokens are Fernet-encrypted at rest. Fall back to the raw value
    # for legacy rows that predate the migration (they will be re-encrypted
    # by `encrypt_legacy_integration_tokens`).
    from cryptography.fernet import InvalidToken
    from th2agent.helpers.encryptor import decrypt_value

    try:
        return decrypt_value(integration.refresh_token)
    except InvalidToken:
        import logging
        logging.getLogger(__name__).warning(
            "Google Drive refresh_token for user_id=%s is plaintext (pre-B7) — "
            "run `python -m th2agent.cli.migrate_integrations --encrypt-legacy`.",
            current_user.user_id,
        )
        return integration.refresh_token


# ── List folder contents ──────────────────────────────────────────────────────

@router.get("/list")
async def list_files(
    folder_id: str | None = Query(None),
    mime_type: str | None = Query(None),
    max_results: int = Query(100, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return files in a Google Drive folder (root by default)."""
    try:
        refresh_token = await _resolve_google_drive_refresh_token(current_user, db)
    except RuntimeError as exc:
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=401)

    async with env_scope(
        {"GOOGLE_DRIVE_REFRESH_TOKEN": refresh_token},
        lock=_gdrive_env_lock,
    ):
        return tool_list_files(
            max_results=max_results,
            folder_id=folder_id,
            mime_type=mime_type,
        )


# ── Search ────────────────────────────────────────────────────────────────────

@router.get("/search")
async def search_files(
    q: str = Query(..., min_length=1),
    max_results: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Search for files across Google Drive by keyword."""
    try:
        refresh_token = await _resolve_google_drive_refresh_token(current_user, db)
    except RuntimeError as exc:
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=401)

    async with env_scope(
        {"GOOGLE_DRIVE_REFRESH_TOKEN": refresh_token},
        lock=_gdrive_env_lock,
    ):
        return tool_search_files(query=q, max_results=max_results)


# ── Download file content as base64 ──────────────────────────────────────────

@router.get("/content")
async def get_file_content(
    file_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Fetch a Google Drive file by ID and return its contents as a base64
    data-URI so the browser can construct a File object.

    Google Workspace files (Docs, Sheets, Slides) are exported to their
    equivalent portable format before encoding.

    Size limit: 20 MB.
    """
    try:
        refresh_token = await _resolve_google_drive_refresh_token(current_user, db)
    except RuntimeError as exc:
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=401)

    # Acquire a short-lived access token under the env scope (minimal time
    # holding the lock); the returned bearer header is safe to reuse after
    # the scope releases because access tokens are short-lived JWTs keyed
    # only to this user's refresh_token.
    try:
        async with env_scope(
            {"GOOGLE_DRIVE_REFRESH_TOKEN": refresh_token},
            lock=_gdrive_env_lock,
        ):
            headers = google_auth_headers(_SERVICE_PREFIX)
    except RuntimeError as exc:
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=401)

    # ── 1. Fetch metadata ─────────────────────────────────────────────────────
    try:
        meta_resp = httpx.get(
            f"{_DRIVE_BASE}/files/{file_id}",
            headers=headers,
            params={"fields": "id,name,mimeType,size"},
            timeout=20,
        )
    except httpx.TimeoutException:
        return JSONResponse(
            {"status": "error", "message": "Metadata request timed out."},
            status_code=504,
        )

    if meta_resp.status_code == 404:
        return JSONResponse(
            {"status": "error", "message": "File not found in Google Drive."},
            status_code=404,
        )
    if meta_resp.status_code == 401:
        return JSONResponse(
            {"status": "error", "message": "Authentication expired. The user needs to reconnect Google Drive."},
            status_code=401,
        )
    if meta_resp.status_code != 200:
        return JSONResponse(
            {"status": "error", "message": f"Drive API returned {meta_resp.status_code}."},
            status_code=502,
        )

    meta = meta_resp.json()
    name = meta.get("name", "file")
    mime = meta.get("mimeType", "application/octet-stream")
    size = int(meta.get("size") or 0)

    # Google Workspace files report size=0; allow them through (export is typically small)
    if size > _MAX_DOWNLOAD_BYTES:
        return JSONResponse(
            {
                "status": "error",
                "message": (
                    f"File is {size / 1_048_576:.1f} MB — exceeds the 20 MB chat attachment limit."
                ),
            },
            status_code=413,
        )

    # ── 2. Download content ───────────────────────────────────────────────────
    try:
        export_mime = _EXPORT_MIME_TYPES.get(mime)
        if export_mime:
            # Google Workspace file: export to portable format
            content_resp = httpx.get(
                f"{_DRIVE_BASE}/files/{file_id}/export",
                headers=headers,
                params={"mimeType": export_mime},
                timeout=60,
            )
            mime = export_mime  # report the exported MIME type to the browser
        else:
            # Regular file: direct download
            content_resp = httpx.get(
                f"{_DRIVE_BASE}/files/{file_id}",
                headers=headers,
                params={"alt": "media"},
                timeout=60,
            )
    except httpx.TimeoutException:
        return JSONResponse(
            {"status": "error", "message": "File download timed out."},
            status_code=504,
        )

    if content_resp.status_code != 200:
        return JSONResponse(
            {"status": "error", "message": f"Download failed with HTTP {content_resp.status_code}."},
            status_code=502,
        )

    # ── 3. Return as base64 data-URI ──────────────────────────────────────────
    b64 = base64.b64encode(content_resp.content).decode()
    return {
        "status": "success",
        "name": name,
        "mimeType": mime,
        "size": len(content_resp.content),
        "base64": f"data:{mime};base64,{b64}",
    }

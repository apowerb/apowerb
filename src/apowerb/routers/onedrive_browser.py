from __future__ import annotations

import asyncio
import base64

import httpx
from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apowerb.auth.dependencies import get_current_user
from apowerb.helpers.database import get_db
from apowerb.helpers.env_scope import env_scope
from apowerb.models import Integration, User
from apowerb.tools_store.portfolio.integration_status import IntegrationStatusError

# Auth/token helpers live in onedrive_core; tools live in onedrive_read
from apowerb.tools_store.portfolio.onedrive_core import (
    _GRAPH_BASE,
    _graph_headers,
    shared_download_and_parse,
)
from apowerb.tools_store.portfolio.onedrive_read import (
    tool_list_files,
    tool_search_files,
)

router = APIRouter(prefix="/api/onedrivebrowser", tags=["onedrive-browser"])

_MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024  # 20 MB hard limit for chat attachments

# B6 — Serialize access to ONEDRIVE_REFRESH_TOKEN so two concurrent users
# never observe each other's refresh_token while their sync tool call is
# running.
_onedrive_env_lock = asyncio.Lock()


async def _resolve_onedrive_refresh_token(
    current_user: User,
    db: AsyncSession,
) -> str:
    """Return the refresh_token stored for *current_user*'s OneDrive
    integration, or raise ``RuntimeError`` if missing. No env mutation here —
    callers pass the returned token to :func:`env_scope`.
    """
    result = await db.execute(
        select(Integration).where(
            Integration.user_id == current_user.user_id,
            Integration.provider == "microsoft_onedrive",
        )
    )
    integration = result.scalar_one_or_none()

    if not integration or not integration.refresh_token:
        raise RuntimeError(
            "OneDrive credentials are not configured. "
            "The user must connect their OneDrive account via the integrations settings page. "
            "Do not retry — inform the user they need to connect OneDrive."
        )

    # B7 — Tokens are Fernet-encrypted at rest. Fall back to the raw value
    # for legacy rows that predate the migration.
    from cryptography.fernet import InvalidToken
    from apowerb.helpers.encryptor import decrypt_value

    try:
        return decrypt_value(integration.refresh_token)
    except InvalidToken:
        import logging
        logging.getLogger(__name__).warning(
            "OneDrive refresh_token for user_id=%s is plaintext (pre-B7) — "
            "run `python -m apowerb.cli.migrate_integrations --encrypt-legacy`.",
            current_user.user_id,
        )
        return integration.refresh_token


# ── List folder contents ──────────────────────────────────────────────────────

@router.get("/list")
async def list_files(
    folder_id: str | None = Query(None),
    folder_path: str | None = Query(None),
    file_type: str | None = Query(None),
    top: int = Query(100, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return files/folders in a OneDrive directory (root by default)."""
    try:
        refresh_token = await _resolve_onedrive_refresh_token(current_user, db)
    except IntegrationStatusError as exc:
        return JSONResponse(
            {
                "status": "error",
                "message": exc.message,
                "code": exc.code,
                "provider": exc.provider,
                "remediable_by_reconnect": exc.is_remediable_by_reconnect,
            },
            status_code=exc.http_status_code,
        )
    except RuntimeError as exc:
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=401)

    async with env_scope(
        {"ONEDRIVE_REFRESH_TOKEN": refresh_token},
        lock=_onedrive_env_lock,
    ):
        return tool_list_files(
            folder_id=folder_id,
            folder_path=folder_path,
            file_type=file_type,
            top=top,
        )


# ── Search ────────────────────────────────────────────────────────────────────

@router.get("/search")
async def search_files(
    q: str = Query(..., min_length=1),
    top: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Search for files across the drive by keyword."""
    try:
        refresh_token = await _resolve_onedrive_refresh_token(current_user, db)
    except IntegrationStatusError as exc:
        return JSONResponse(
            {
                "status": "error",
                "message": exc.message,
                "code": exc.code,
                "provider": exc.provider,
                "remediable_by_reconnect": exc.is_remediable_by_reconnect,
            },
            status_code=exc.http_status_code,
        )
    except RuntimeError as exc:
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=401)

    async with env_scope(
        {"ONEDRIVE_REFRESH_TOKEN": refresh_token},
        lock=_onedrive_env_lock,
    ):
        return tool_search_files(query=q, top=top)


# ── Download file content as base64 ──────────────────────────────────────────

@router.get("/content")
async def get_file_content(
    item_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Fetch a OneDrive file by item ID and return its contents as a
    base64 data-URI so the browser can construct a File object without
    exposing the pre-authenticated download URL.

    Size limit: 20 MB. Larger files should be downloaded via
    tool_download_file instead.
    """
    try:
        refresh_token = await _resolve_onedrive_refresh_token(current_user, db)
    except IntegrationStatusError as exc:
        return JSONResponse(
            {
                "status": "error",
                "message": exc.message,
                "code": exc.code,
                "provider": exc.provider,
                "remediable_by_reconnect": exc.is_remediable_by_reconnect,
            },
            status_code=exc.http_status_code,
        )
    except RuntimeError as exc:
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=401)

    # Acquire short-lived access token under env scope, then release the
    # lock — bearer headers are safe to reuse after scope exit.
    try:
        async with env_scope(
            {"ONEDRIVE_REFRESH_TOKEN": refresh_token},
            lock=_onedrive_env_lock,
        ):
            headers = _graph_headers()
    except IntegrationStatusError as exc:
        return JSONResponse(
            {
                "status": "error",
                "message": exc.message,
                "code": exc.code,
                "provider": exc.provider,
                "remediable_by_reconnect": exc.is_remediable_by_reconnect,
            },
            status_code=exc.http_status_code,
        )
    except RuntimeError as exc:
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=401)

    # ── 1. Fetch metadata WITHOUT $select ─────────────────────────────────────
    # @microsoft.graph.downloadUrl is an instance annotation that Graph silently
    # drops when $select is used. Fetching without $select guarantees it's present.
    try:
        meta_resp = httpx.get(
            f"{_GRAPH_BASE}/me/drive/items/{item_id}",
            headers=headers,
            timeout=20,
        )
    except httpx.TimeoutException:
        return JSONResponse(
            {"status": "error", "message": "Metadata request timed out."},
            status_code=504,
        )

    if meta_resp.status_code == 404:
        return JSONResponse(
            {"status": "error", "message": "File not found in OneDrive."},
            status_code=404,
        )
    if meta_resp.status_code != 200:
        return JSONResponse(
            {"status": "error", "message": f"Graph API returned {meta_resp.status_code}."},
            status_code=502,
        )

    meta = meta_resp.json()

    if "folder" in meta:
        return JSONResponse(
            {"status": "error", "message": "The selected item is a folder, not a file."},
            status_code=400,
        )

    name = meta.get("name", "file")
    mime = (meta.get("file") or {}).get("mimeType", "application/octet-stream")
    size = meta.get("size", 0)

    if size > _MAX_DOWNLOAD_BYTES:
        return JSONResponse(
            {
                "status": "error",
                "message": (
                    f"File is {size / 1_048_576:.1f} MB — exceeds the 20 MB chat attachment limit. "
                    "Use tool_download_file to save it locally instead."
                ),
            },
            status_code=413,
        )

    # ── 2. Download content via /content endpoint (follows redirect with auth) -
    # This is more reliable than @microsoft.graph.downloadUrl which can expire
    # or be absent for certain item types.
    try:
        content_resp = httpx.get(
            f"{_GRAPH_BASE}/me/drive/items/{item_id}/content",
            headers=headers,
            timeout=60,
            follow_redirects=True,
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
        "size": size,
        "base64": f"data:{mime};base64,{b64}",
    }


# ── Excel / CSV preview for the chart wizard ─────────────────────────────────


@router.get("/excel-preview")
async def get_excel_preview(
    item_path: str = Query(..., min_length=1),
    sheet_name: str | None = Query(None),
    limit: int = Query(5, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Download a OneDrive Excel/CSV file and return its column names plus
    the first ``limit`` rows.

    Used by the frontend chart wizard to render a tabular preview *before*
    a chart is created. Graph credentials follow the same pattern as the
    other routes in this router: resolve the user's OneDrive
    ``Integration`` row, acquire a short-lived bearer header under
    :func:`env_scope`, then call
    :func:`shared_download_and_parse` off the event loop.
    """
    if not item_path.strip():
        return JSONResponse(
            {"status": "error", "message": "item_path is required."},
            status_code=400,
        )

    try:
        refresh_token = await _resolve_onedrive_refresh_token(current_user, db)
    except IntegrationStatusError as exc:
        return JSONResponse(
            {
                "status": "error",
                "message": exc.message,
                "code": exc.code,
                "provider": exc.provider,
                "remediable_by_reconnect": exc.is_remediable_by_reconnect,
            },
            status_code=exc.http_status_code,
        )
    except RuntimeError as exc:
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=401)

    # Acquire bearer header under env scope, then release the lock.
    try:
        async with env_scope(
            {"ONEDRIVE_REFRESH_TOKEN": refresh_token},
            lock=_onedrive_env_lock,
        ):
            headers = _graph_headers()
    except IntegrationStatusError as exc:
        return JSONResponse(
            {
                "status": "error",
                "message": exc.message,
                "code": exc.code,
                "provider": exc.provider,
                "remediable_by_reconnect": exc.is_remediable_by_reconnect,
            },
            status_code=exc.http_status_code,
        )
    except RuntimeError as exc:
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=401)

    # Normalise sheet_name: pandas accepts str or int. Digit strings must be
    # converted to int so "1" selects the second worksheet (not a sheet named
    # literally "1").
    sheet_arg: str | int | None = sheet_name
    if isinstance(sheet_arg, str):
        stripped = sheet_arg.strip()
        if not stripped:
            sheet_arg = None
        elif stripped.isdigit():
            sheet_arg = int(stripped)
        else:
            sheet_arg = stripped

    try:
        df, err = await asyncio.to_thread(
            shared_download_and_parse,
            item_path.strip(),
            headers,
            sheet_name=sheet_arg,
        )
    except Exception as exc:
        return JSONResponse(
            {
                "status": "error",
                "message": f"Failed to download OneDrive file: {exc}",
            },
            status_code=502,
        )

    if err is not None or df is None:
        return JSONResponse(
            {
                "status": "error",
                "message": err or "Unable to parse the OneDrive file.",
            },
            status_code=422,
        )

    # Clean NaN before serialising: JSON can't represent NaN reliably and
    # the wizard expects stringable values.
    clean_df = df.fillna("").head(limit)
    return {
        "columns": list(clean_df.columns.astype(str)),
        "rows": clean_df.to_dict(orient="records"),
    }
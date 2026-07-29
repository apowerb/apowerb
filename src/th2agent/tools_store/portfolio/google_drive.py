"""Google Drive tools -- List, search, read, and download files via Drive API.

Provides 5 tools for account info, listing, searching, reading content,
and downloading files from a connected Google Drive account.

Auth credentials are injected as environment variables by the tool_config
system at agent runtime:
  - ``GOOGLE_DRIVE_REFRESH_TOKEN`` -- OAuth2 refresh token

Client ID and client secret are read from application settings at runtime
(not stored per-tool).

The shared helper ``google_auth`` transparently exchanges the refresh token
for a short-lived access token and caches it for ~50 minutes.
"""

import os
import re
from logging import getLogger

import httpx

from th2agent.configs.paths import uploads_dir

from th2agent.tools_store.portfolio.google_auth import google_auth_headers
from th2agent.tools_store.portfolio.integration_status import IntegrationStatusError

logger = getLogger(__name__)

_DRIVE_BASE = "https://www.googleapis.com/drive/v3"
_SERVICE_PREFIX = "GOOGLE_DRIVE"

# Fields returned for file listings
_FILE_FIELDS = "files(id,name,mimeType,size,modifiedTime,webViewLink)"

# Google Workspace MIME types that support export
_EXPORT_MIME_TYPES = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
    "application/vnd.google-apps.drawing": "image/png",
}

# MIME types considered text-readable
_TEXT_MIME_PREFIXES = ("text/", "application/json", "application/xml", "application/javascript")

_MAX_TEXT_CONTENT_LENGTH = 10000


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _sanitize_filename(name: str) -> str:
    """Strip path components and dangerous characters from a filename."""
    name = os.path.basename(name)
    name = re.sub(r'[^\w\s\-.]', '_', name)
    if not name or name.startswith('.'):
        name = f"drive_file_{name}"
    return name


def _format_file(f: dict) -> dict:
    """Format a Drive API file object into a consistent dict."""
    return {
        "id": f.get("id"),
        "name": f.get("name"),
        "mimeType": f.get("mimeType"),
        "size": f.get("size"),
        "modifiedTime": f.get("modifiedTime"),
        "webViewLink": f.get("webViewLink"),
    }


# ---------------------------------------------------------------------------
# Public tools (auto-discovered via the ``tool_`` prefix)
# ---------------------------------------------------------------------------


def tool_get_account_info() -> dict:
    """Get information about the connected Google Drive account.

    Returns the account owner's email address, display name, and storage
    quota.  Use this tool when the user asks about the Google Drive account
    itself (owner, email, storage usage) rather than about specific files.

    Returns:
        dict with keys: status, email, display_name, photo_link,
        storage_quota (dict with limit, usage, usage_in_drive,
        usage_in_trash -- all in bytes).
    """
    try:
        headers = google_auth_headers(_SERVICE_PREFIX)
    except IntegrationStatusError as e:
        return e.as_tool_result()
    except RuntimeError as e:
        return {"status": "error", "message": str(e), "retry": False}

    try:
        resp = httpx.get(
            f"{_DRIVE_BASE}/about",
            headers=headers,
            params={"fields": "user,storageQuota"},
            timeout=30,
        )
        if resp.status_code == 401:
            return {
                "status": "error",
                "message": "Authentication expired. The user needs to reconnect their Google account. Do not retry.",
                "retry": False,
            }
        if resp.status_code >= 400:
            return {
                "status": "error",
                "message": f"Drive API error (HTTP {resp.status_code}): {resp.text[:300]}. Do not retry — inform the user.",
                "retry": False,
            }

        data = resp.json()
        user = data.get("user", {})
        quota = data.get("storageQuota", {})
        return {
            "status": "success",
            "email": user.get("emailAddress"),
            "display_name": user.get("displayName"),
            "photo_link": user.get("photoLink"),
            "storage_quota": {
                "limit": quota.get("limit"),
                "usage": quota.get("usage"),
                "usage_in_drive": quota.get("usageInDrive"),
                "usage_in_trash": quota.get("usageInDriveTrash"),
            },
        }
    except httpx.TimeoutException:
        return {
            "status": "error",
            "message": "Request timed out. You may retry once.",
            "retry": True,
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed to get account info: {e}. Do not retry — inform the user.",
            "retry": False,
        }


def tool_list_files(
    max_results: int = 10,
    folder_id: str | None = None,
    mime_type: str | None = None,
) -> dict:
    """List files from Google Drive.

    Retrieves recent files, optionally filtered by folder or MIME type.

    Args:
        max_results: Maximum number of files to return (1-100). Defaults to 10.
        folder_id: If provided, only list files inside this folder.
            Use the folder's Drive ID (e.g. from a previous search).
        mime_type: If provided, only return files of this MIME type.
            Common values: "application/vnd.google-apps.document" (Google Docs),
            "application/vnd.google-apps.spreadsheet" (Sheets),
            "application/pdf", "image/png".

    Returns:
        dict with keys: status, files (list), total.
    """
    try:
        headers = google_auth_headers(_SERVICE_PREFIX)
    except IntegrationStatusError as e:
        return e.as_tool_result()
    except RuntimeError as e:
        return {"status": "error", "message": str(e), "retry": False}

    max_results = max(1, min(max_results, 100))
    params: dict[str, str] = {
        "pageSize": str(max_results),
        "fields": _FILE_FIELDS,
        "orderBy": "modifiedTime desc",
    }

    # Build query filter
    q_parts: list[str] = ["trashed = false"]
    if folder_id:
        q_parts.append(f"'{folder_id}' in parents")
    if mime_type:
        q_parts.append(f"mimeType = '{mime_type}'")
    params["q"] = " and ".join(q_parts)

    try:
        resp = httpx.get(
            f"{_DRIVE_BASE}/files",
            headers=headers,
            params=params,
            timeout=30,
        )
        if resp.status_code == 401:
            return {
                "status": "error",
                "message": "Authentication expired. The user needs to reconnect their Google account. Do not retry.",
                "retry": False,
            }
        if resp.status_code >= 400:
            return {
                "status": "error",
                "message": f"Drive API error (HTTP {resp.status_code}): {resp.text[:300]}. Do not retry — inform the user.",
                "retry": False,
            }

        data = resp.json()
        files = [_format_file(f) for f in data.get("files", [])]
        return {"status": "success", "files": files, "total": len(files)}
    except httpx.TimeoutException:
        return {
            "status": "error",
            "message": "Request timed out. The Drive service may be slow. You may retry once.",
            "retry": True,
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed to list files: {e}. Do not retry — inform the user.",
            "retry": False,
        }


def tool_search_files(query: str, max_results: int = 10) -> dict:
    """Search files in Google Drive by name or content.

    Uses Drive's full-text search to find files matching the query across
    file names and file content.

    Args:
        query: Search query string. Searches file names and content
            (e.g. "quarterly report", "budget 2026").
        max_results: Maximum number of results to return (1-100). Defaults to 10.

    Returns:
        dict with keys: status, files (list), total.
    """
    if not query or not query.strip():
        return {
            "status": "error",
            "message": "Search query is required. Do not retry with an empty value.",
            "retry": False,
        }

    try:
        headers = google_auth_headers(_SERVICE_PREFIX)
    except IntegrationStatusError as e:
        return e.as_tool_result()
    except RuntimeError as e:
        return {"status": "error", "message": str(e), "retry": False}

    max_results = max(1, min(max_results, 100))
    # Escape single quotes in query for Drive API
    safe_query = query.replace("\\", "\\\\").replace("'", "\\'")
    params: dict[str, str] = {
        "q": f"fullText contains '{safe_query}' and trashed = false",
        "pageSize": str(max_results),
        "fields": _FILE_FIELDS,
        "orderBy": "modifiedTime desc",
    }

    try:
        resp = httpx.get(
            f"{_DRIVE_BASE}/files",
            headers=headers,
            params=params,
            timeout=30,
        )
        if resp.status_code == 401:
            return {
                "status": "error",
                "message": "Authentication expired. The user needs to reconnect their Google account. Do not retry.",
                "retry": False,
            }
        if resp.status_code >= 400:
            return {
                "status": "error",
                "message": f"Drive API error (HTTP {resp.status_code}): {resp.text[:300]}. Do not retry — inform the user.",
                "retry": False,
            }

        data = resp.json()
        files = [_format_file(f) for f in data.get("files", [])]
        return {"status": "success", "files": files, "total": len(files)}
    except httpx.TimeoutException:
        return {
            "status": "error",
            "message": "Request timed out. You may retry once.",
            "retry": True,
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed to search files: {e}. Do not retry — inform the user.",
            "retry": False,
        }


def tool_read_drive_file(file_id: str) -> dict:
    """Read the content or metadata of a Google Drive file.

    For Google Workspace files (Docs, Sheets, Slides), exports the content
    as plain text. For other text files, returns the raw content (up to
    10,000 characters). For binary files, returns metadata and a download link.

    Args:
        file_id: The Drive file ID (from tool_list_files or tool_search_files).

    Returns:
        dict with keys: status, id, name, mimeType, content (text) or
        webViewLink (for binary files).
    """
    if not file_id or not file_id.strip():
        return {
            "status": "error",
            "message": "file_id is required. Do not retry with an empty value.",
            "retry": False,
        }

    try:
        headers = google_auth_headers(_SERVICE_PREFIX)
    except IntegrationStatusError as e:
        return e.as_tool_result()
    except RuntimeError as e:
        return {"status": "error", "message": str(e), "retry": False}

    try:
        # Step 1: Get file metadata
        meta_resp = httpx.get(
            f"{_DRIVE_BASE}/files/{file_id}",
            headers=headers,
            params={"fields": "id,name,mimeType,size,modifiedTime,webViewLink"},
            timeout=30,
        )
        if meta_resp.status_code == 401:
            return {
                "status": "error",
                "message": "Authentication expired. The user needs to reconnect their Google account. Do not retry.",
                "retry": False,
            }
        if meta_resp.status_code == 404:
            return {
                "status": "error",
                "message": f"File not found (file_id={file_id}). It may have been deleted or you don't have access. Do not retry with the same ID.",
                "retry": False,
            }
        if meta_resp.status_code >= 400:
            return {
                "status": "error",
                "message": f"Drive API error (HTTP {meta_resp.status_code}): {meta_resp.text[:300]}. Do not retry — inform the user.",
                "retry": False,
            }

        meta = meta_resp.json()
        mime_type = meta.get("mimeType", "")
        result = {
            "status": "success",
            "id": meta.get("id"),
            "name": meta.get("name"),
            "mimeType": mime_type,
            "modifiedTime": meta.get("modifiedTime"),
            "webViewLink": meta.get("webViewLink"),
        }

        # Step 2: Get content based on MIME type
        # Google Workspace files: export as text
        export_mime = _EXPORT_MIME_TYPES.get(mime_type)
        if export_mime:
            content_resp = httpx.get(
                f"{_DRIVE_BASE}/files/{file_id}/export",
                headers=headers,
                params={"mimeType": export_mime},
                timeout=60,
            )
            if content_resp.status_code == 200:
                content = content_resp.text
                if len(content) > _MAX_TEXT_CONTENT_LENGTH:
                    content = content[:_MAX_TEXT_CONTENT_LENGTH] + "\n\n... [content truncated at 10,000 characters]"
                result["content"] = content
            else:
                result["content"] = f"[Export failed with HTTP {content_resp.status_code}. Use the webViewLink to view the file.]"
            return result

        # Regular files: try to read content
        is_text = any(mime_type.startswith(prefix) for prefix in _TEXT_MIME_PREFIXES)
        if is_text:
            content_resp = httpx.get(
                f"{_DRIVE_BASE}/files/{file_id}",
                headers=headers,
                params={"alt": "media"},
                timeout=60,
            )
            if content_resp.status_code == 200:
                content = content_resp.text
                if len(content) > _MAX_TEXT_CONTENT_LENGTH:
                    content = content[:_MAX_TEXT_CONTENT_LENGTH] + "\n\n... [content truncated at 10,000 characters]"
                result["content"] = content
            else:
                result["content"] = f"[Download failed with HTTP {content_resp.status_code}. Use the webViewLink to view the file.]"
            return result

        # Binary file: return metadata only
        result["content"] = (
            f"[Binary file ({mime_type}). Use tool_download_file to download it, "
            f"or open it via the webViewLink.]"
        )
        return result

    except httpx.TimeoutException:
        return {
            "status": "error",
            "message": "Request timed out. You may retry once.",
            "retry": True,
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed to read file: {e}. Do not retry — inform the user.",
            "retry": False,
        }


def tool_download_file(file_id: str, save_filename: str | None = None) -> dict:
    """Download a file from Google Drive and save it locally.

    Downloads the file content and saves it to the agent's uploads directory.
    For Google Workspace files (Docs, Sheets, Slides), exports as PDF.

    Args:
        file_id: The Drive file ID to download.
        save_filename: Optional filename to save as. If omitted, uses the
            original file name from Drive.

    Returns:
        dict with keys: status, path, file_name, size.
    """
    if not file_id or not file_id.strip():
        return {
            "status": "error",
            "message": "file_id is required. Do not retry with an empty value.",
            "retry": False,
        }

    try:
        headers = google_auth_headers(_SERVICE_PREFIX)
    except IntegrationStatusError as e:
        return e.as_tool_result()
    except RuntimeError as e:
        return {"status": "error", "message": str(e), "retry": False}

    try:
        # Step 1: Get file metadata
        meta_resp = httpx.get(
            f"{_DRIVE_BASE}/files/{file_id}",
            headers=headers,
            params={"fields": "id,name,mimeType,size"},
            timeout=30,
        )
        if meta_resp.status_code == 401:
            return {
                "status": "error",
                "message": "Authentication expired. The user needs to reconnect their Google account. Do not retry.",
                "retry": False,
            }
        if meta_resp.status_code == 404:
            return {
                "status": "error",
                "message": f"File not found (file_id={file_id}). Do not retry with the same ID.",
                "retry": False,
            }
        if meta_resp.status_code >= 400:
            return {
                "status": "error",
                "message": f"Drive API error (HTTP {meta_resp.status_code}): {meta_resp.text[:300]}. Do not retry — inform the user.",
                "retry": False,
            }

        meta = meta_resp.json()
        mime_type = meta.get("mimeType", "")
        original_name = meta.get("name", "downloaded_file")

        # Step 2: Download content
        export_mime = _EXPORT_MIME_TYPES.get(mime_type)
        if export_mime:
            # Google Workspace file: export as PDF for download
            download_resp = httpx.get(
                f"{_DRIVE_BASE}/files/{file_id}/export",
                headers=headers,
                params={"mimeType": "application/pdf"},
                timeout=120,
            )
            if not save_filename:
                # Add .pdf extension for exported files
                base_name = os.path.splitext(original_name)[0]
                save_filename = f"{base_name}.pdf"
        else:
            # Regular file: direct download
            download_resp = httpx.get(
                f"{_DRIVE_BASE}/files/{file_id}",
                headers=headers,
                params={"alt": "media"},
                timeout=120,
            )

        if download_resp.status_code == 401:
            return {
                "status": "error",
                "message": "Authentication expired. The user needs to reconnect their Google account. Do not retry.",
                "retry": False,
            }
        if download_resp.status_code >= 400:
            return {
                "status": "error",
                "message": f"Drive API download error (HTTP {download_resp.status_code}): {download_resp.text[:300]}. Do not retry — inform the user.",
                "retry": False,
            }

        content = download_resp.content
        file_name = _sanitize_filename(save_filename or original_name)

        # Save to uploads directory.
        # Folder convention: ``uploads/agent{id}/`` — must match the agent_name
        # used by factory-bound tools (pdf_to_images, read_uploaded_file,
        # create_downloadable_file). Otherwise downloaded files are invisible
        # to subsequent tool calls.
        agent_id = os.getenv("ROOT_AGENT_ID", "unknown_agent")
        save_dir = str(uploads_dir() / f"agent{agent_id}")
        os.makedirs(save_dir, exist_ok=True)

        file_path = os.path.join(save_dir, file_name)

        # Validate that resolved path stays within save_dir
        if not os.path.abspath(file_path).startswith(os.path.abspath(save_dir)):
            return {
                "status": "error",
                "message": "Invalid filename. Do not retry.",
                "retry": False,
            }

        with open(file_path, "wb") as f:
            f.write(content)

        abs_path = os.path.abspath(file_path)
        logger.info("Drive file saved: %s (%d bytes)", abs_path, len(content))

        return {
            "status": "success",
            "path": abs_path,
            "file_name": file_name,
            "size": len(content),
        }
    except httpx.TimeoutException:
        return {
            "status": "error",
            "message": "Download timed out. The file may be too large. You may retry once.",
            "retry": True,
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed to download file: {e}. Do not retry — inform the user.",
            "retry": False,
        }

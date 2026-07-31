import os
from logging import getLogger

import httpx

from apowerb.configs.paths import uploads_dir

from apowerb.tools_store.portfolio.integration_status import IntegrationStatusError
from apowerb.tools_store.portfolio.onedrive_core import (
    _GRAPH_BASE,
    _MAX_READ_CHARS,
    _TYPE_KEYWORD_TO_EXTENSIONS,
    _format_item,
    _graph_headers,
    _handle_graph_error,
    _is_readable,
    _sanitize_filename,
)

logger = getLogger(__name__)


def tool_list_files(
    folder_id: str | None = None,
    folder_path: str | None = None,
    file_type: str | None = None,
    top: int = 50,
) -> dict:
    """List files and folders inside a OneDrive directory.

    Starts at the drive root when neither ``folder_id`` nor ``folder_path``
    is provided. Use this to browse the user's OneDrive hierarchy before
    fetching a specific file.

    Args:
        folder_id:   Item ID of the folder to list (from a previous call).
            Takes precedence over ``folder_path`` when both are given.
        folder_path: Path relative to drive root, e.g. "Documents/Reports"
            or "Microsoft Teams Chat Files". Spaces are handled automatically.
            Ignored if ``folder_id`` is provided.
        file_type:   If provided, only return files of this type.
            Use the extension keyword: "csv", "pdf", "xlsx", "docx", "png", etc.
            Filtering is done by file extension (reliable across all MIME assignments).
            Folders are always included regardless of this filter.
        top: Maximum number of items to return (1–200). Defaults to 50.

    Returns:
        dict with keys: status, items (list), total.
        Each item: id, name, type (file/folder), size, mimeType,
        lastModified, createdAt, webUrl, parentPath, childCount.
    """
    from urllib.parse import quote as _quote

    try:
        headers = _graph_headers()
    except IntegrationStatusError as e:
        return e.as_tool_result()
    except RuntimeError as e:
        return {"status": "error", "message": str(e), "retry": False}

    top = max(1, min(top, 200))

    if folder_id:
        url = f"{_GRAPH_BASE}/me/drive/items/{folder_id}/children"
    elif folder_path:
        clean_path = folder_path.strip("/")
        encoded_path = _quote(clean_path, safe="/")
        url = f"{_GRAPH_BASE}/me/drive/root:/{encoded_path}:/children"
    else:
        url = f"{_GRAPH_BASE}/me/drive/items/root/children"

    # Resolve extension set from file_type keyword (extension-based, not MIME-based)
    allowed_exts: set[str] | None = None
    if file_type:
        allowed_exts = _TYPE_KEYWORD_TO_EXTENSIONS.get(file_type.lower().lstrip("."))

    params: dict[str, str] = {
        "$top":     str(top),
        "$select":  "id,name,size,file,folder,lastModifiedDateTime,createdDateTime,webUrl,parentReference",
        "$orderby": "name asc",
    }

    try:
        resp = httpx.get(url, headers=headers, params=params, timeout=30)
        err = _handle_graph_error(resp, "list files")
        if err:
            return err

        items = [_format_item(i) for i in resp.json().get("value", [])]

        # Client-side extension filter — keeps folders, strips wrong-type files.
        if allowed_exts:
            items = [
                i for i in items
                if i.get("type") == "folder"
                or os.path.splitext(i.get("name", "").lower())[1] in allowed_exts
            ]

        return {
            "status": "success",
            "items": items,
            "total": len(items),
            "folder_exists": True,
            "note": "total=0 means the folder exists but is empty — do NOT create it.",
        }

    except httpx.TimeoutException:
        return {"status": "error", "message": "Request timed out. You may retry once.", "retry": True}
    except Exception as e:
        return {"status": "error", "message": f"Failed to list files: {e}. Do not retry — inform the user.", "retry": False}


def tool_get_file_metadata(
    item_id: str | None = None,
    item_path: str | None = None,
) -> dict:
    """Get detailed metadata for a specific file or folder.

    Provide either ``item_id`` (preferred, from tool_list_files) or
    ``item_path`` (relative path from drive root).

    Args:
        item_id:   The unique OneDrive item ID.
        item_path: Path relative to drive root, e.g. "Documents/report.pdf".

    Returns:
        dict with keys: status, id, name, type, size, mimeType,
        lastModified, createdAt, webUrl, parentPath, childCount,
        downloadUrl (for files only — pre-authenticated, valid ~1 hour).
    """
    if not item_id and not item_path:
        return {
            "status":  "error",
            "message": "Provide either item_id or item_path. Do not retry without one of them.",
            "retry":   False,
        }

    try:
        headers = _graph_headers()
    except IntegrationStatusError as e:
        return e.as_tool_result()
    except RuntimeError as e:
        return {"status": "error", "message": str(e), "retry": False}

    from urllib.parse import quote as _quote
    if item_id:
        url = f"{_GRAPH_BASE}/me/drive/items/{item_id}"
    else:
        clean_path = _quote((item_path or "").strip("/"), safe="/")
        url = f"{_GRAPH_BASE}/me/drive/root:/{clean_path}"

    try:
        resp = httpx.get(
            url,
            headers=headers,
            params={
                "$select": "id,name,size,file,folder,lastModifiedDateTime,createdDateTime,webUrl,parentReference,@microsoft.graph.downloadUrl",
            },
            timeout=20,
        )
        err = _handle_graph_error(resp, "get file metadata")
        if err:
            return err

        item = resp.json()
        result = _format_item(item)
        result["downloadUrl"] = item.get("@microsoft.graph.downloadUrl")
        return {"status": "success", **result}

    except httpx.TimeoutException:
        return {"status": "error", "message": "Request timed out. You may retry once.", "retry": True}
    except Exception as e:
        return {"status": "error", "message": f"Failed to get file metadata: {e}. Do not retry — inform the user.", "retry": False}


def tool_search_files(
    query: str,
    top: int = 25,
) -> dict:
    """Search for files and folders in OneDrive by name or content keywords.

    Uses Microsoft Graph's DriveItem search which matches against file names,
    content (for indexed files), and metadata. Results are filtered client-side
    by file extension when the query is a known file type keyword.

    Note: Wildcards (* ?) are not supported by the Graph search API and will
    be stripped automatically. Use plain keywords only (e.g. "budget",
    "invoice 2025"). To find files by type, just use the type name as the
    query (e.g. "csv", "pdf", "xlsx", "png").

    Args:
        query: The search term (e.g. "budget 2025", "invoice", "csv", "pdf").
               Do NOT include wildcards — just plain keywords.
               If the query is a file type keyword (csv, pdf, xlsx, png, etc.)
               results will be automatically filtered to that extension only.
        top:   Maximum number of results to return (1–100). Defaults to 25.

    Returns:
        dict with keys: status, items (list), total.
        Each item: id, name, type, size, mimeType, lastModified, webUrl, parentPath.
    """
    # Strip wildcards — Graph search does not support them and returns errors
    query = query.replace("*", "").replace("?", "").strip()

    if not query:
        return {
            "status":  "error",
            "message": "query must not be empty (wildcards like * are not supported — use plain keywords). Do not retry without a value.",
            "retry":   False,
        }

    # Auto-resolve file extensions from type keyword.
    allowed_exts: set[str] | None = _TYPE_KEYWORD_TO_EXTENSIONS.get(query.lower())

    try:
        headers = _graph_headers()
    except IntegrationStatusError as e:
        return e.as_tool_result()
    except RuntimeError as e:
        return {"status": "error", "message": str(e), "retry": False}

    top = max(1, min(top, 100))

    # Fetch 3× more from Graph when extension-filtering so we don't under-return
    fetch_top = min(top * 3, 200) if allowed_exts else top

    try:
        resp = httpx.get(
            f"{_GRAPH_BASE}/me/drive/root/search(q='{query}')",
            headers=headers,
            params={
                "$top":    str(fetch_top),
                "$select": "id,name,size,file,folder,lastModifiedDateTime,createdDateTime,webUrl,parentReference",
            },
            timeout=30,
        )
        err = _handle_graph_error(resp, "search files")
        if err:
            return err

        items = [_format_item(i) for i in resp.json().get("value", [])]

        # Client-side extension filter
        if allowed_exts:
            items = [
                i for i in items
                if os.path.splitext(i.get("name", "").lower())[1] in allowed_exts
            ]

        items = items[:top]
        return {"status": "success", "items": items, "total": len(items)}

    except httpx.TimeoutException:
        return {"status": "error", "message": "Request timed out. You may retry once.", "retry": True}
    except Exception as e:
        return {"status": "error", "message": f"Failed to search files: {e}. Do not retry — inform the user.", "retry": False}


def tool_read_onedrive_file(
    item_id: str | None = None,
    item_path: str | None = None,
) -> dict:
    """Read the content of a OneDrive file directly into the agent context.

    Supports plain-text formats: .txt, .md, .csv, .json, .xml, .yaml, .log,
    .ini, .cfg, .toml, .env, and any file with a text/* or application/json
    MIME type. Content is capped at 20 000 characters — a ``truncated: true``
    flag is set when the file is larger.

    For binary files (PDF, images, Office docs, etc.) this tool returns
    metadata and a ``webUrl`` instead of failing — use tool_download_file
    to save those locally.

    Args:
        item_id:   The unique OneDrive item ID (preferred).
        item_path: Path relative to drive root, e.g. "notes/todo.md".

    Returns:
        For text files — dict with keys: status, name, mime_type, size_bytes,
            content, truncated.
        For binary files — dict with keys: status, name, mime_type, size_bytes,
            webUrl, note (directing the user to download instead).
    """
    if not item_id and not item_path:
        return {
            "status":  "error",
            "message": "Provide either item_id or item_path. Do not retry without one of them.",
            "retry":   False,
        }

    try:
        headers = _graph_headers()
    except IntegrationStatusError as e:
        return e.as_tool_result()
    except RuntimeError as e:
        return {"status": "error", "message": str(e), "retry": False}

    from urllib.parse import quote as _quote
    if item_id:
        meta_url     = f"{_GRAPH_BASE}/me/drive/items/{item_id}"
        download_url = f"{_GRAPH_BASE}/me/drive/items/{item_id}/content"
    else:
        clean_path   = _quote((item_path or "").strip("/"), safe="/")
        meta_url     = f"{_GRAPH_BASE}/me/drive/root:/{clean_path}"
        download_url = f"{_GRAPH_BASE}/me/drive/root:/{clean_path}:/content"

    try:
        meta_resp = httpx.get(
            meta_url,
            headers=headers,
            params={"$select": "id,name,file,folder,size,webUrl"},
            timeout=20,
        )
        err = _handle_graph_error(meta_resp, "resolve file for read")
        if err:
            return err

        meta = meta_resp.json()

        if "folder" in meta:
            return {
                "status":  "error",
                "message": "The specified item is a folder. Use tool_list_files to browse its contents.",
                "retry":   False,
            }

        file_name = meta.get("name", "")
        mime_type = (meta.get("file") or {}).get("mimeType", "application/octet-stream")
        size      = meta.get("size", 0)
        ext       = os.path.splitext(file_name)[1].lower()

        # Download the raw bytes (needed for all formats)
        dl_resp = httpx.get(download_url, headers=headers, timeout=60, follow_redirects=True)
        err = _handle_graph_error(dl_resp, "read file content")
        if err:
            return err

        raw_bytes = dl_resp.content

        # xlsx / xls → parse with pandas and return as readable table
        if ext in (".xlsx", ".xls"):
            try:
                import io
                import pandas as pd
                df = pd.read_excel(io.BytesIO(raw_bytes), engine="openpyxl")
                content = df.to_csv(index=False)
                truncated = len(content) > _MAX_READ_CHARS
                return {
                    "status":     "success",
                    "name":       file_name,
                    "mime_type":  mime_type,
                    "size_bytes": size,
                    "format":     "excel_as_csv",
                    "rows":       len(df),
                    "columns":    list(df.columns),
                    "content":    content[:_MAX_READ_CHARS] if truncated else content,
                    "truncated":  truncated,
                }
            except Exception as e:
                return {
                    "status":    "error",
                    "name":      file_name,
                    "mime_type": mime_type,
                    "message":   f"Could not parse Excel file: {e}. Try tool_download_file instead.",
                }

        # csv → read as text directly
        if ext == ".csv":
            try:
                content = raw_bytes.decode("utf-8")
            except UnicodeDecodeError:
                content = raw_bytes.decode("latin-1", errors="replace")
            truncated = len(content) > _MAX_READ_CHARS
            return {
                "status":     "success",
                "name":       file_name,
                "mime_type":  mime_type,
                "size_bytes": size,
                "content":    content[:_MAX_READ_CHARS] if truncated else content,
                "truncated":  truncated,
            }

        # Other binary (images, pdf, docx, etc.) → return metadata + webUrl
        if not _is_readable(meta):
            return {
                "status":     "success",
                "name":       file_name,
                "mime_type":  mime_type,
                "size_bytes": size,
                "webUrl":     meta.get("webUrl"),
                "note": (
                    f"'{file_name}' is a binary file ({mime_type}) and cannot be read as text. "
                    "Open it via webUrl or use tool_download_file to save it locally."
                ),
            }

        # Plain text files
        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            text = raw_bytes.decode("latin-1", errors="replace")

        truncated = len(text) > _MAX_READ_CHARS
        content   = text[:_MAX_READ_CHARS] if truncated else text

        return {
            "status":     "success",
            "name":       file_name,
            "mime_type":  mime_type,
            "size_bytes": len(raw_bytes),
            "content":    content,
            "truncated":  truncated,
        }

    except httpx.TimeoutException:
        return {"status": "error", "message": "Request timed out. You may retry once.", "retry": True}
    except Exception as e:
        return {"status": "error", "message": f"Failed to read file: {e}. Do not retry — inform the user.", "retry": False}


def tool_download_file(
    item_id: str | None = None,
    item_path: str | None = None,
    save_filename: str | None = None,
) -> dict:
    """Download a OneDrive file to the agent's uploads directory.

    Provide either ``item_id`` (preferred) or ``item_path``. After downloading,
    the absolute local path is returned so other tools can process the file.

    Args:
        item_id:       The unique OneDrive item ID (from tool_list_files or tool_search_files).
        item_path:     Path relative to drive root, e.g. "Documents/report.pdf".
        save_filename: Optional filename override. Defaults to the original file name.

    Returns:
        dict with keys: status, file_path, file_name, size_bytes, mime_type.
    """
    if not item_id and not item_path:
        return {
            "status":  "error",
            "message": "Provide either item_id or item_path. Do not retry without one of them.",
            "retry":   False,
        }

    try:
        headers = _graph_headers()
    except IntegrationStatusError as e:
        return e.as_tool_result()
    except RuntimeError as e:
        return {"status": "error", "message": str(e), "retry": False}

    from urllib.parse import quote as _quote
    if item_id:
        meta_url     = f"{_GRAPH_BASE}/me/drive/items/{item_id}"
        download_url = f"{_GRAPH_BASE}/me/drive/items/{item_id}/content"
    else:
        clean_path   = _quote((item_path or "").strip("/"), safe="/")
        meta_url     = f"{_GRAPH_BASE}/me/drive/root:/{clean_path}"
        download_url = f"{_GRAPH_BASE}/me/drive/root:/{clean_path}:/content"

    try:
        meta_resp = httpx.get(
            meta_url,
            headers=headers,
            params={"$select": "id,name,file,size"},
            timeout=20,
        )
        err = _handle_graph_error(meta_resp, "resolve file for download")
        if err:
            return err

        meta = meta_resp.json()

        if "folder" in meta:
            return {
                "status":  "error",
                "message": "The specified item is a folder, not a file. Use tool_list_files to browse its contents.",
                "retry":   False,
            }

        original_name = meta.get("name", "onedrive_file")
        mime_type     = (meta.get("file") or {}).get("mimeType", "application/octet-stream")
        file_name     = _sanitize_filename(save_filename or original_name)

        dl_resp = httpx.get(download_url, headers=headers, timeout=120, follow_redirects=True)
        err = _handle_graph_error(dl_resp, "download file content")
        if err:
            return err

        content = dl_resp.content

        # Folder convention: ``uploads/agent{id}/`` — must match the agent_name
        # used by factory-bound tools (pdf_to_images, read_uploaded_file,
        # create_downloadable_file). Otherwise downloaded files are invisible
        # to subsequent tool calls.
        agent_id = os.getenv("ROOT_AGENT_ID", "unknown_agent")
        agent_folder = f"agent{agent_id}"
        save_dir = str(uploads_dir() / agent_folder)
        os.makedirs(save_dir, exist_ok=True)

        file_path = os.path.join(save_dir, file_name)

        if not os.path.abspath(file_path).startswith(os.path.abspath(save_dir)):
            return {"status": "error", "message": "Invalid filename. Do not retry.", "retry": False}

        with open(file_path, "wb") as f:
            f.write(content)

        abs_path = os.path.abspath(file_path)

        # Also upload to S3 so read_uploaded_file can find it in S3 mode
        try:
            from apowerb.configs.settings import get_settings as _get_settings
            if _get_settings().storage_mode != "local":
                s3_key = f"uploads/{agent_folder}/{file_name}"
                from apowerb.tools_store.portfolio.onedrive_core import upload_bytes_to_s3
                upload_bytes_to_s3(content, s3_key)
                logger.info("OneDrive file mirrored to S3: %s", s3_key)
        except Exception as _s3_err:
            logger.warning("Could not mirror file to S3: %s", _s3_err)

        logger.info("OneDrive file saved: %s (%d bytes)", abs_path, len(content))

        return {
            "status":     "success",
            "file_path":  abs_path,
            "file_name":  file_name,
            "size_bytes": len(content),
            "mime_type":  mime_type,
        }

    except httpx.TimeoutException:
        return {"status": "error", "message": "Download timed out. The file may be too large. You may retry once.", "retry": True}
    except Exception as e:
        return {"status": "error", "message": f"Failed to download file: {e}. Do not retry — inform the user.", "retry": False}


def tool_list_shared_files(top: int = 25) -> dict:
    """List files and folders that have been shared with the current user by others.

    Returns items from other users' drives that have been explicitly shared,
    including files shared via Microsoft Teams chat. Self-shared items are excluded.

    Args:
        top: Maximum number of items to return (1–100). Defaults to 25.

    Returns:
        dict with keys: status, items (list), total.
        Each item: id, name, type, size, mimeType, lastModified,
        webUrl, sharedBy (name + email), parentPath.
    """
    try:
        headers = _graph_headers()
    except IntegrationStatusError as e:
        return e.as_tool_result()
    except RuntimeError as e:
        return {"status": "error", "message": str(e), "retry": False}

    top = max(1, min(top, 100))

    # Fetch current user identity to filter out self-shared items.
    my_email = ""
    try:
        me_resp = httpx.get(
            f"{_GRAPH_BASE}/me",
            headers=headers,
            params={"$select": "mail,userPrincipalName"},
            timeout=10,
        )
        if me_resp.status_code == 200:
            me_data  = me_resp.json()
            my_email = (me_data.get("mail") or me_data.get("userPrincipalName") or "").lower()
    except Exception:
        pass

    items: list[dict] = []
    seen_ids: set[str] = set()

    def _add_item(entry: dict, shared_by_name: str = "", shared_by_email: str = "") -> None:
        """Normalise a driveItem/remoteItem and append it to `items`."""
        remote    = entry.get("remoteItem") or entry
        item_id   = entry.get("id", "")
        if item_id in seen_ids:
            return
        seen_ids.add(item_id)

        if not shared_by_name and not shared_by_email:
            shared_obj      = (remote.get("shared") or {}).get("sharedBy", {})
            user_obj        = shared_obj.get("user", {})
            shared_by_name  = user_obj.get("displayName") or ""
            shared_by_email = (user_obj.get("email") or "").lower()

        if my_email and shared_by_email and shared_by_email == my_email:
            return

        is_folder = "folder" in remote
        items.append({
            "id":           item_id,
            "name":         remote.get("name"),
            "type":         "folder" if is_folder else "file",
            "size":         remote.get("size"),
            "mimeType":     remote.get("file", {}).get("mimeType") if not is_folder else None,
            "lastModified": remote.get("lastModifiedDateTime"),
            "webUrl":       remote.get("webUrl") or entry.get("webUrl"),
            "sharedBy": {
                "name":  shared_by_name,
                "email": shared_by_email,
            },
            "parentPath": remote.get("parentReference", {}).get("path"),
        })

    # Source 1: /me/drive/sharedWithMe
    # NOTE: This endpoint does NOT support $top — we fetch everything and trim.
    try:
        resp = httpx.get(
            f"{_GRAPH_BASE}/me/drive/sharedWithMe",
            headers=headers,
            timeout=30,
        )
        if resp.status_code == 200:
            for entry in resp.json().get("value", []):
                _add_item(entry)
    except Exception:
        pass

    # Source 2: /me/insights/shared
    # Covers Teams-chat-shared files that often don't appear in sharedWithMe.
    try:
        ins_resp = httpx.get(
            f"{_GRAPH_BASE}/me/insights/shared",
            headers=headers,
            params={"$top": str(top * 2)},
            timeout=30,
        )
        if ins_resp.status_code == 200:
            for insight in ins_resp.json().get("value", []):
                resource_ref = insight.get("resourceReference", {})
                last_shared  = (insight.get("lastShared") or {})
                shared_by    = (last_shared.get("sharedBy") or {})
                viz          = insight.get("resourceVisualization", {})

                shared_by_name  = shared_by.get("displayName", "")
                shared_by_email = (shared_by.get("address") or "").lower()

                entry = {
                    "id":                   resource_ref.get("id", ""),
                    "name":                 viz.get("title", resource_ref.get("id", "")),
                    "size":                 None,
                    "mimeType":             viz.get("type", ""),
                    "lastModifiedDateTime": last_shared.get("sharedDateTime", ""),
                    "webUrl":               resource_ref.get("webUrl", ""),
                    "folder":               {} if viz.get("containerType") == "Folder" else None,
                    "file":                 {"mimeType": viz.get("type", "")} if viz.get("containerType") != "Folder" else None,
                    "parentReference":      {"path": viz.get("containerDisplayName", "")},
                }
                _add_item(entry, shared_by_name=shared_by_name, shared_by_email=shared_by_email)
    except Exception:
        pass

    if not items:
        return {
            "status": "success",
            "items": [],
            "total": 0,
            "note": "No shared files found. Files shared via Teams may take time to appear, or the sharing scope may require additional permissions.",
        }

    return {"status": "success", "items": items[:top], "total": min(len(items), top)}
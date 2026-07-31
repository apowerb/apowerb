import io
import os
from logging import getLogger

import httpx

from apowerb.tools_store.portfolio.integration_status import IntegrationStatusError
from apowerb.tools_store.portfolio.onedrive_core import (
    _GRAPH_BASE,
    _graph_headers,
    _handle_graph_error,
    _sanitize_filename,
)

logger = getLogger(__name__)


def tool_upload_file(
    local_file_path: str,
    destination_path: str | None = None,
    destination_folder_id: str | None = None,
    conflict_behavior: str = "rename",
) -> dict:
    """Upload a local file to OneDrive.

    Uses the Graph API upload session for files of any size (handles large
    files automatically via chunked upload for files > 4 MB).

    Args:
        local_file_path:      Absolute or relative path to the local file to upload.
        destination_path:     Path in OneDrive where the file should land, relative
                              to the drive root. E.g. "Documents/Reports/report.pdf".
                              If omitted, the file is uploaded to the root with its
                              original name. Takes precedence over destination_folder_id.
        destination_folder_id: Item ID of the destination folder. Used when you have
                               the folder ID from a previous tool_list_files call.
                               Ignored if destination_path is provided.
        conflict_behavior:    What to do if a file with the same name already exists.
                              "rename"  – upload as a new copy with a unique name (default).
                              "replace" – overwrite the existing file.
                              "fail"    – return an error without uploading.

    Returns:
        dict with keys: status, id, name, size_bytes, webUrl, parentPath.
    """
    from urllib.parse import quote as _quote

    if not os.path.exists(local_file_path):
        return {
            "status":  "error",
            "message": f"Local file not found: '{local_file_path}'. Do not retry without a valid path.",
            "retry":   False,
        }

    if conflict_behavior not in {"rename", "replace", "fail"}:
        conflict_behavior = "rename"

    try:
        headers = _graph_headers()
    except IntegrationStatusError as e:
        return e.as_tool_result()
    except RuntimeError as e:
        return {"status": "error", "message": str(e), "retry": False}

    file_name = os.path.basename(local_file_path)
    file_size = os.path.getsize(local_file_path)

    if destination_path:
        clean = _quote(destination_path.strip("/"), safe="/")
        upload_url  = f"{_GRAPH_BASE}/me/drive/root:/{clean}:/content"
        session_url = f"{_GRAPH_BASE}/me/drive/root:/{clean}:/createUploadSession"
    elif destination_folder_id:
        enc_name    = _quote(file_name, safe="")
        upload_url  = f"{_GRAPH_BASE}/me/drive/items/{destination_folder_id}:/{enc_name}:/content"
        session_url = f"{_GRAPH_BASE}/me/drive/items/{destination_folder_id}:/{enc_name}:/createUploadSession"
    else:
        enc_name    = _quote(file_name, safe="")
        upload_url  = f"{_GRAPH_BASE}/me/drive/root:/{enc_name}:/content"
        session_url = f"{_GRAPH_BASE}/me/drive/root:/{enc_name}:/createUploadSession"

    try:
        # Small files (≤ 4 MB): simple PUT
        if file_size <= 4 * 1024 * 1024:
            with open(local_file_path, "rb") as f:
                data = f.read()
            resp = httpx.put(
                upload_url,
                content=data,
                headers={
                    **headers,
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(len(data)),
                },
                params={"@microsoft.graph.conflictBehavior": conflict_behavior},
                timeout=60,
            )
            err = _handle_graph_error(resp, "upload file")
            if err:
                return err
            item = resp.json()

        # Large files (> 4 MB): upload session with 10 MB chunks
        else:
            session_resp = httpx.post(
                session_url,
                json={"item": {"@microsoft.graph.conflictBehavior": conflict_behavior}},
                headers={**headers, "Content-Type": "application/json"},
                timeout=30,
            )
            err = _handle_graph_error(session_resp, "create upload session")
            if err:
                return err

            upload_session_url = session_resp.json().get("uploadUrl")
            if not upload_session_url:
                return {"status": "error", "message": "No uploadUrl in session response.", "retry": False}

            chunk_size = 10 * 1024 * 1024  # 10 MB chunks
            item = None
            with open(local_file_path, "rb") as f:
                offset = 0
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    end = offset + len(chunk) - 1
                    chunk_resp = httpx.put(
                        upload_session_url,
                        content=chunk,
                        headers={
                            "Content-Range": f"bytes {offset}-{end}/{file_size}",
                            "Content-Length": str(len(chunk)),
                        },
                        timeout=120,
                    )
                    if chunk_resp.status_code in (200, 201):
                        item = chunk_resp.json()
                    elif chunk_resp.status_code == 202:
                        pass  # continue uploading
                    else:
                        return {
                            "status":  "error",
                            "message": f"Chunk upload failed at byte {offset}: HTTP {chunk_resp.status_code}",
                            "retry":   True,
                        }
                    offset += len(chunk)

            if not item:
                return {"status": "error", "message": "Upload completed but no item metadata returned.", "retry": False}

        logger.info("Uploaded '%s' to OneDrive (%d bytes)", file_name, file_size)
        return {
            "status":     "success",
            "id":         item.get("id"),
            "name":       item.get("name"),
            "size_bytes": item.get("size", file_size),
            "webUrl":     item.get("webUrl"),
            "parentPath": (item.get("parentReference") or {}).get("path"),
        }

    except httpx.TimeoutException:
        return {"status": "error", "message": "Upload timed out. You may retry once.", "retry": True}
    except Exception as e:
        return {"status": "error", "message": f"Failed to upload file: {e}. Do not retry — inform the user.", "retry": False, "detail": str(e)}


def tool_create_folder(
    folder_name: str,
    parent_folder_id: str | None = None,
    parent_folder_path: str | None = None,
    conflict_behavior: str = "fail",
) -> dict:
    """Create a new folder in OneDrive.

    Args:
        folder_name:         Name of the new folder.
        parent_folder_id:    Item ID of the parent folder. If omitted and
                             parent_folder_path is also omitted, creates at root.
        parent_folder_path:  Path of the parent folder relative to drive root,
                             e.g. "Documents/Reports". Ignored if parent_folder_id
                             is provided.
        conflict_behavior:   "fail" (default) | "rename" | "replace".
                             Default is "fail" — if the folder already exists this
                             returns an error so you know to use the existing folder
                             instead of accidentally creating a duplicate.

    Returns:
        dict with keys: status, id, name, webUrl, parentPath.
    """
    from urllib.parse import quote as _quote

    if not folder_name or not folder_name.strip():
        return {"status": "error", "message": "folder_name must not be empty.", "retry": False}

    if conflict_behavior not in {"rename", "replace", "fail"}:
        conflict_behavior = "fail"

    try:
        headers = _graph_headers()
    except IntegrationStatusError as e:
        return e.as_tool_result()
    except RuntimeError as e:
        return {"status": "error", "message": str(e), "retry": False}

    if parent_folder_id:
        url = f"{_GRAPH_BASE}/me/drive/items/{parent_folder_id}/children"
    elif parent_folder_path:
        clean = _quote(parent_folder_path.strip("/"), safe="/")
        url = f"{_GRAPH_BASE}/me/drive/root:/{clean}:/children"
    else:
        url = f"{_GRAPH_BASE}/me/drive/root/children"

    body = {
        "name":   folder_name.strip(),
        "folder": {},
        "@microsoft.graph.conflictBehavior": conflict_behavior,
    }

    try:
        resp = httpx.post(
            url,
            json=body,
            headers={**headers, "Content-Type": "application/json"},
            timeout=20,
        )
        err = _handle_graph_error(resp, "create folder")
        if err:
            return err

        item = resp.json()
        logger.info("Created OneDrive folder '%s'", folder_name)
        return {
            "status":     "success",
            "id":         item.get("id"),
            "name":       item.get("name"),
            "webUrl":     item.get("webUrl"),
            "parentPath": (item.get("parentReference") or {}).get("path"),
        }

    except httpx.TimeoutException:
        return {"status": "error", "message": "Request timed out. You may retry once.", "retry": True}
    except Exception as e:
        return {"status": "error", "message": f"Failed to create folder: {e}. Do not retry — inform the user.", "retry": False}


def tool_update_file(
    item_id: str | None = None,
    item_path: str | None = None,
    # ── Plain file overwrite ───────────────────────────────────────────────
    text_content: str | None = None,
    local_file_path: str | None = None,
    # ── Structured updates (xlsx / csv) ───────────────────────────────────
    set_column_all_rows: dict[str, str] | None = None,
    update_row: int | None = None,
    update_row_values: dict[str, str] | None = None,
    filter_column: str | None = None,
    filter_value: str | None = None,
    filter_update_values: dict[str, str] | None = None,
) -> dict:
    """Universal file updater for OneDrive. Handles any file type.

    Identify the target file with ``item_id`` (preferred) or ``item_path``.

    ── MODE 1: Full overwrite ─────────────────────────────────────────────────
    Replace entire file content. Use for .txt, .csv, .json, .md, or any binary.
        text_content      → new content as a UTF-8 string
        local_file_path   → path to a local file (xlsx, pdf, image, etc.)

    ── MODE 2: Structured updates (xlsx / csv only) ──────────────────────────
    Surgically edit rows/columns without rewriting the whole file.
    Modes 2a, 2b, 2c can be combined in a single call.

    2a) Set a column to the same value on ALL rows:
        set_column_all_rows = {"Status": "pending"}

    2b) Update specific cells in ONE row by row number (1-based):
        update_row=3, update_row_values={"Status": "sent", "Sent at": "2026-03-16 11:25"}

    2c) Update rows matching a filter condition:
        filter_column="Email", filter_value="a@b.com",
        filter_update_values={"Status": "sent"}

    Modes 2a/2b/2c create new columns automatically if they don't exist.

    Args:
        item_id:              OneDrive item ID (preferred).
        item_path:            Path relative to drive root, e.g. "Sales/comptes.xlsx".
        text_content:         New full content as a string (mode 1 — text files).
        local_file_path:      Local file path for full binary replacement (mode 1).
        set_column_all_rows:  {column: value} — set column to same value for all rows (mode 2a).
        update_row:           1-based row index to update (mode 2b).
        update_row_values:    {column: value} dict for the specific row (mode 2b).
        filter_column:        Column name to match on (mode 2c).
        filter_value:         Value to match in filter_column (mode 2c).
        filter_update_values: {column: value} dict to apply on matching rows (mode 2c).

    Returns:
        dict with keys: status, name, webUrl, and details about what was changed.
    """
    from urllib.parse import quote as _quote

    if not item_id and not item_path:
        return {"status": "error", "message": "Provide item_id or item_path.", "retry": False}

    is_overwrite  = text_content is not None or local_file_path is not None
    is_structured = set_column_all_rows or update_row is not None or filter_column

    if is_overwrite and is_structured:
        return {"status": "error",
                "message": "Cannot combine full overwrite (text_content/local_file_path) with structured updates.",
                "retry": False}

    if not is_overwrite and not is_structured:
        return {"status": "error",
                "message": "Provide at least one update: text_content, local_file_path, set_column_all_rows, update_row/update_row_values, or filter_column/filter_value/filter_update_values.",
                "retry": False}

    if local_file_path and not os.path.exists(local_file_path):
        return {"status": "error", "message": f"Local file not found: '{local_file_path}'.", "retry": False}

    try:
        headers = _graph_headers()
    except IntegrationStatusError as e:
        return e.as_tool_result()
    except RuntimeError as e:
        return {"status": "error", "message": str(e), "retry": False}

    if item_id:
        meta_url     = f"{_GRAPH_BASE}/me/drive/items/{item_id}"
        download_url = f"{_GRAPH_BASE}/me/drive/items/{item_id}/content"
        upload_url   = f"{_GRAPH_BASE}/me/drive/items/{item_id}/content"
    else:
        clean        = _quote((item_path or "").strip("/"), safe="/")
        meta_url     = f"{_GRAPH_BASE}/me/drive/root:/{clean}"
        download_url = f"{_GRAPH_BASE}/me/drive/root:/{clean}:/content"
        upload_url   = f"{_GRAPH_BASE}/me/drive/root:/{clean}:/content"

    try:
        meta_resp = httpx.get(meta_url, headers=headers,
                              params={"$select": "id,name,file"}, timeout=20)
        err = _handle_graph_error(meta_resp, "resolve file")
        if err:
            return err
        file_name = meta_resp.json().get("name", "file")
        ext = os.path.splitext(file_name)[1].lower()

        # ══ MODE 1: Full overwrite ═════════════════════════════════════════
        if is_overwrite:
            if text_content is not None:
                data = text_content.encode("utf-8")
            else:
                with open(local_file_path, "rb") as f:  # type: ignore[arg-type]
                    data = f.read()

            resp = httpx.put(upload_url, content=data, headers={
                **headers,
                "Content-Type": "application/octet-stream",
            }, timeout=120)
            err = _handle_graph_error(resp, "overwrite file")
            if err:
                return err

            item = resp.json()
            logger.info("Overwrote OneDrive file '%s' (%d bytes)", file_name, len(data))
            return {
                "status":     "success",
                "name":       file_name,
                "size_bytes": item.get("size", len(data)),
                "webUrl":     item.get("webUrl"),
                "mode":       "full_overwrite",
            }

        # ══ MODE 2: Structured updates (xlsx / csv) ════════════════════════
        if ext not in (".xlsx", ".xls", ".csv"):
            return {"status": "error",
                    "message": f"Structured updates only work on xlsx/xls/csv files. '{file_name}' is '{ext}'. Use text_content for other file types.",
                    "retry": False}

        try:
            import pandas as pd
        except ImportError:
            return {"status": "error",
                    "message": "pandas or openpyxl not installed. Run: pip install pandas openpyxl",
                    "retry": False}

        # Download current content
        dl_resp = httpx.get(download_url, headers=headers, timeout=120, follow_redirects=True)
        err = _handle_graph_error(dl_resp, "download file for structured update")
        if err:
            return err

        if ext == ".csv":
            df = pd.read_csv(io.BytesIO(dl_resp.content))
        else:
            df = pd.read_excel(io.BytesIO(dl_resp.content), engine="openpyxl")

        changes = []

        # 2a) Set column to same value on ALL rows
        if set_column_all_rows:
            for col, val in set_column_all_rows.items():
                df[col] = val
                changes.append(f"set '{col}'='{val}' on all {len(df)} rows")

        # 2b) Update a specific row by 1-based index
        if update_row is not None:
            if not update_row_values:
                return {"status": "error",
                        "message": "update_row requires update_row_values dict.",
                        "retry": False}
            pandas_idx = update_row - 1
            if pandas_idx < 0 or pandas_idx >= len(df):
                return {"status": "error",
                        "message": f"update_row={update_row} is out of range — file has {len(df)} data rows.",
                        "retry": False}
            for col, val in update_row_values.items():
                if col not in df.columns:
                    df[col] = None
                if isinstance(val, str) and df[col].dtype != object:
                    df[col] = df[col].astype(object)
                df.at[pandas_idx, col] = val
            changes.append(f"updated row {update_row}: {update_row_values}")

        # 2c) Update rows matching a filter
        if filter_column:
            if not filter_value or not filter_update_values:
                return {"status": "error",
                        "message": "filter_column requires both filter_value and filter_update_values.",
                        "retry": False}
            if filter_column not in df.columns:
                return {"status": "error",
                        "message": f"filter_column '{filter_column}' not found. Available columns: {list(df.columns)}",
                        "retry": False}
            mask = df[filter_column].astype(str) == str(filter_value)
            matched = int(mask.sum())
            for col, val in filter_update_values.items():
                if col not in df.columns:
                    df[col] = None
                if isinstance(val, str) and df[col].dtype != object:
                    df[col] = df[col].astype(object)
                df.loc[mask, col] = val
            changes.append(f"updated {matched} rows where {filter_column}='{filter_value}': {filter_update_values}")

        if not changes:
            return {"status": "error", "message": "No changes were made — check your parameters.", "retry": False}

        # Serialize back to bytes
        buf = io.BytesIO()
        if ext == ".csv":
            df.to_csv(buf, index=False)
        else:
            df.to_excel(buf, index=False, engine="openpyxl")
        buf.seek(0)
        new_bytes = buf.read()

        logger.info("Uploading modified '%s' (%d bytes) back to OneDrive", file_name, len(new_bytes))

        up_resp = httpx.put(upload_url, content=new_bytes, headers={
            **headers,
            "Content-Type": "application/octet-stream",
        }, timeout=180)

        # Retry up to 5 times with increasing delays for 423 (file locked by OneDrive sync)
        if up_resp.status_code == 423:
            import time as _time
            _retry_delays = [3, 5, 10, 15, 20]
            for _delay in _retry_delays:
                logger.warning("File locked (423), retrying in %s seconds...", _delay)
                _time.sleep(_delay)
                up_resp = httpx.put(upload_url, content=new_bytes, headers={
                    **headers,
                    "Content-Type": "application/octet-stream",
                }, timeout=180)
                if up_resp.status_code != 423:
                    logger.info("File unlock succeeded after retry (HTTP %s)", up_resp.status_code)
                    break
                logger.warning("Still locked (423) after %s seconds delay", _delay)

        if up_resp.status_code not in (200, 201):
            logger.error("Re-upload failed HTTP %s: %s", up_resp.status_code, up_resp.text[:300])
            err = _handle_graph_error(up_resp, "re-upload after structured update")
            if err:
                return err

        try:
            item = up_resp.json()
        except Exception:
            item = {}

        logger.info("Structured update on '%s': %s", file_name, "; ".join(changes))
        return {
            "status":       "success",
            "name":         file_name,
            "webUrl":       item.get("webUrl"),
            "mode":         "structured_update",
            "changes":      changes,
            "rows_in_file": len(df),
        }

    except httpx.TimeoutException:
        return {"status": "error", "message": "Request timed out. You may retry once.", "retry": True}
    except Exception as e:
        return {"status": "error", "message": f"Failed to update file: {e}. Do not retry — inform the user.", "retry": False}


def tool_delete_item(
    item_id: str | None = None,
    item_path: str | None = None,
) -> dict:
    """Permanently delete a file or folder from OneDrive.

    WARNING: Deletion via the Graph API sends the item to the recycle bin
    (recoverable from OneDrive web UI for 30 days by default).

    Provide either ``item_id`` (preferred, safer) or ``item_path``.

    Args:
        item_id:   The unique OneDrive item ID.
        item_path: Path relative to drive root, e.g. "Documents/old_report.pdf".

    Returns:
        dict with keys: status, message.
    """
    from urllib.parse import quote as _quote

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

    if item_id:
        url = f"{_GRAPH_BASE}/me/drive/items/{item_id}"
    else:
        clean = _quote((item_path or "").strip("/"), safe="/")
        url = f"{_GRAPH_BASE}/me/drive/root:/{clean}"

    try:
        resp = httpx.delete(url, headers=headers, timeout=20)

        if resp.status_code == 204:
            logger.info("Deleted OneDrive item: %s", item_id or item_path)
            return {
                "status":  "success",
                "message": "Item deleted successfully (moved to recycle bin).",
            }

        err = _handle_graph_error(resp, "delete item")
        if err:
            return err

        return {"status": "success", "message": "Item deleted."}

    except httpx.TimeoutException:
        return {"status": "error", "message": "Request timed out. You may retry once.", "retry": True}
    except Exception as e:
        return {"status": "error", "message": f"Failed to delete item: {e}. Do not retry — inform the user.", "retry": False}
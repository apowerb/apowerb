import io
import os
import re
from logging import getLogger

import httpx

from apowerb.tools_store.portfolio.integration_status import (
    INTEGRATION_BLOCKED_BY_TENANT,
    IntegrationStatusError,
)

logger = getLogger(__name__)

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"


# Text-based MIME types we can safely decode and return as a string
_READABLE_MIME_TYPES: set[str] = {
    "text/plain",
    "text/csv",
    "text/markdown",
    "text/html",
    "application/json",
    "application/xml",
    "text/xml",
    "application/x-yaml",
    "text/yaml",
}

# File extensions we treat as readable regardless of MIME type
_READABLE_EXTENSIONS: set[str] = {
    ".txt", ".md", ".csv", ".json", ".xml", ".yaml", ".yml",
    ".log", ".ini", ".cfg", ".toml", ".env",
}

# Maximum number of characters returned when reading a file inline
_MAX_READ_CHARS = 20_000

# Common type keywords the agent might use → file extensions to match against.
# We filter by extension (not MIME) because Graph assigns inconsistent MIME types
# to common formats — notably .csv files get "application/vnd.ms-excel" rather
# than "text/csv", which would break MIME-based filtering.
_TYPE_KEYWORD_TO_EXTENSIONS: dict[str, set[str]] = {
    "csv":   {".csv"},
    "pdf":   {".pdf"},
    "txt":   {".txt"},
    "md":    {".md", ".markdown"},
    "json":  {".json"},
    "xml":   {".xml"},
    "yaml":  {".yaml", ".yml"},
    "png":   {".png"},
    "jpg":   {".jpg", ".jpeg"},
    "jpeg":  {".jpg", ".jpeg"},
    "gif":   {".gif"},
    "xlsx":  {".xlsx"},
    "xls":   {".xls"},
    "excel": {".xlsx", ".xls", ".csv"},
    "spreadsheet": {".xlsx", ".xls", ".xlsm", ".ods", ".csv", ".tsv"},
    "docx":  {".docx"},
    "doc":   {".doc", ".docx"},
    "word":  {".doc", ".docx"},
    "pptx":  {".pptx"},
    "ppt":   {".ppt", ".pptx"},
    "zip":   {".zip"},
    "mp4":   {".mp4"},
    "mp3":   {".mp3"},
    "image": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"},
    "video": {".mp4", ".mov", ".avi", ".mkv"},
}


# ---------------------------------------------------------------------------
# Token bootstrap
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_access_token() -> str:
    """Return a fresh Microsoft Graph access token for OneDrive.

    Delegates to the invoker-scoped :mod:`microsoft_auth` helper: the token is
    resolved and cached per invoker (never via a shared process-global), which
    closes the concurrent-invocation race on ``ONEDRIVE_REFRESH_TOKEN``
    (incident 2026-07-03). ``env_scope`` callers that set the global
    deliberately are still honoured (ONEDRIVE resolves env-first).

    Raises:
        IntegrationStatusError: ``INTEGRATION_MISSING`` when OneDrive is not
            connected, ``INTEGRATION_EXPIRED`` when the refresh token is
            revoked, ``INTEGRATION_ERROR`` for transient token-endpoint
            failures.
    """
    from apowerb.tools_store.portfolio.microsoft_auth import get_microsoft_access_token

    return get_microsoft_access_token(
        "ONEDRIVE",
        scope="offline_access Files.ReadWrite",
        service_label="OneDrive",
    )


def _graph_headers() -> dict[str, str]:
    """Return Authorization header dict for Microsoft Graph calls."""
    return {"Authorization": f"Bearer {_get_access_token()}"}


def _handle_graph_error(resp: httpx.Response, context: str) -> dict | None:
    """Return a structured error dict for non-2xx Graph responses, else None."""
    if resp.status_code == 401:
        logger.error("OneDrive %s — 401 Unauthorized: %s", context, resp.text[:500])
        return {
            "status": "error",
            "message": "Authentication expired. The user needs to reconnect OneDrive. Do not retry.",
            "retry": False,
        }
    if resp.status_code == 403:
        logger.error("OneDrive %s — 403 Forbidden: %s", context, resp.text[:500])
        return {
            "status": "error",
            "message": (
                f"Permission denied for {context} (HTTP 403). "
                "The OneDrive integration may be missing required scopes. Do not retry."
            ),
            "retry": False,
        }
    if resp.status_code == 404:
        logger.error("OneDrive %s — 404 Not Found: %s", context, resp.text[:500])
        return {
            "status": "error",
            "message": (
                f"Resource not found for {context}. "
                "The ID or path may be invalid or deleted. Do not retry with the same value."
            ),
            "retry": False,
        }
    if resp.status_code == 423:
        # The transient file-sync 423 is retried *before* this function is
        # called (see `_upload_with_retry` and write paths). A 423 reaching
        # this point means SharePoint / OneDrive at the *tenant* level has
        # blocked the app — the Microsoft 365 admin must whitelist it via
        # the SharePoint admin center → Access control. Reconnecting will
        # not help, so we surface a structured status code.
        logger.error("OneDrive %s — 423 resourceLocked (tenant policy): %s", context, resp.text[:500])
        return IntegrationStatusError(
            code=INTEGRATION_BLOCKED_BY_TENANT,
            provider="microsoft",
            message=(
                f"OneDrive/SharePoint refused {context} with HTTP 423 "
                "(resourceLocked). The user's Microsoft 365 administrator "
                "must allow this app in SharePoint admin center → Access "
                "control. Reconnecting will not help."
            ),
        ).as_tool_result()
    if resp.status_code >= 400:
        logger.error("OneDrive %s — HTTP %s: %s", context, resp.status_code, resp.text[:500])
        return {
            "status": "error",
            "message": (
                f"Graph API error for {context} (HTTP {resp.status_code}): "
                f"{resp.text[:300]}. Do not retry — inform the user."
            ),
            "retry": False,
        }
    return None


def _sanitize_filename(name: str) -> str:
    """Strip path components and dangerous characters from a filename."""
    name = os.path.basename(name)
    name = re.sub(r"[^\w\s\-.]", "_", name)
    if not name or name.startswith("."):
        name = f"onedrive_{name}"
    return name


def _format_item(item: dict) -> dict:
    """Normalise a Graph driveItem into a clean summary dict."""
    is_folder = "folder" in item
    return {
        "id":           item.get("id"),
        "name":         item.get("name"),
        "type":         "folder" if is_folder else "file",
        "size":         item.get("size"),
        "lastModified": item.get("lastModifiedDateTime"),
        "createdAt":    item.get("createdDateTime"),
        "mimeType":     item.get("file", {}).get("mimeType") if not is_folder else None,
        "webUrl":       item.get("webUrl"),
        "parentPath":   item.get("parentReference", {}).get("path"),
        "childCount":   item.get("folder", {}).get("childCount") if is_folder else None,
    }


def _is_readable(item: dict) -> bool:
    """Return True if a driveItem looks like a text-readable file."""
    mime = (item.get("file") or {}).get("mimeType", "")
    name = item.get("name", "")
    ext  = os.path.splitext(name)[-1].lower()
    return mime in _READABLE_MIME_TYPES or ext in _READABLE_EXTENSIONS


# ---------------------------------------------------------------------------
# Shared utilities (used by campaign_tracker and followup_tracker)
# ---------------------------------------------------------------------------


def shared_download_and_parse(
    item_path: str,
    headers: dict,
    *,
    sheet_name: str | int | None = None,
) -> "tuple[pd.DataFrame | None, str | None]":
    """Download an xlsx or csv file from OneDrive and parse into a DataFrame.

    Args:
        item_path: Path relative to drive root (e.g. "comptes.xlsx").
        headers: Graph API auth headers from ``_graph_headers()``.
        sheet_name: Optional Excel worksheet selector (name or 0-based index).
            Ignored for CSV files. When ``None`` (default), the first sheet
            of the workbook is returned — same behaviour as before the kwarg
            was added, so existing callers are unaffected.

    Returns:
        (DataFrame, None) on success, (None, error_message) on failure.
    """
    import pandas as pd
    from urllib.parse import quote as _quote

    # Tolerate item_path with a leading "/drive/root:" prefix (Graph URL style).
    # Normalise to a plain path relative to the drive root.
    raw = (item_path or "").strip()
    if raw.startswith("/drive/root:"):
        raw = raw[len("/drive/root:"):]
    clean = _quote(raw.strip("/"), safe="/")
    url = f"{_GRAPH_BASE}/me/drive/root:/{clean}:/content"

    resp = httpx.get(url, headers=headers, timeout=60, follow_redirects=True)
    if resp.status_code != 200:
        return None, f"Download failed (HTTP {resp.status_code}): {resp.text[:200]}"

    ext = os.path.splitext(item_path)[1].lower()
    if ext == ".csv":
        return pd.read_csv(io.BytesIO(resp.content)), None
    excel_kwargs: dict = {"engine": "calamine"}
    if sheet_name is not None:
        excel_kwargs["sheet_name"] = sheet_name
    return pd.read_excel(io.BytesIO(resp.content), **excel_kwargs), None


# Extensions supported by :func:`download_and_parse_spreadsheet`.
# ``.xlsx`` / ``.xlsm`` are read with calamine (Rust, ~4x faster than
# openpyxl on 20k-100k row files, same DataFrame); ``.xls`` keeps xlrd;
# ``.ods`` relies on pandas' ``odf`` engine (odfpy), which is an optional
# runtime dependency — we surface a clear error if it's missing.
_SPREADSHEET_EXCEL_EXTS: set[str] = {".xlsx", ".xlsm"}
_SPREADSHEET_LEGACY_XLS: set[str] = {".xls"}
_SPREADSHEET_ODS_EXTS: set[str] = {".ods"}
_SPREADSHEET_CSV_EXTS: set[str] = {".csv"}
_SPREADSHEET_TSV_EXTS: set[str] = {".tsv"}
_SPREADSHEET_ALL_EXTS: set[str] = (
    _SPREADSHEET_EXCEL_EXTS
    | _SPREADSHEET_LEGACY_XLS
    | _SPREADSHEET_ODS_EXTS
    | _SPREADSHEET_CSV_EXTS
    | _SPREADSHEET_TSV_EXTS
)


def _onedrive_download_bytes(
    item_path: str,
    headers: dict,
    *,
    timeout: int = 60,
) -> tuple[bytes | None, str | None]:
    """Download the raw bytes of a OneDrive driveItem.

    Shared helper for :func:`shared_download_and_parse` and
    :func:`download_and_parse_spreadsheet`. Returns ``(bytes, None)`` on
    success and ``(None, error_message)`` on any HTTP failure.
    """
    from urllib.parse import quote as _quote

    raw = (item_path or "").strip()
    if raw.startswith("/drive/root:"):
        raw = raw[len("/drive/root:") :]
    clean = _quote(raw.strip("/"), safe="/")
    url = f"{_GRAPH_BASE}/me/drive/root:/{clean}:/content"

    resp = httpx.get(url, headers=headers, timeout=timeout, follow_redirects=True)
    if resp.status_code != 200:
        return None, f"Download failed (HTTP {resp.status_code}): {resp.text[:200]}"
    return resp.content, None


def download_and_parse_spreadsheet(
    item_path: str,
    headers: dict,
    *,
    sheet_name: str | int | None = None,
) -> "tuple[pd.DataFrame | None, str | None]":
    """Download a OneDrive spreadsheet (xlsx/xls/xlsm/ods/csv/tsv) into a
    DataFrame.

    Routes by file extension:

    - ``.xlsx`` / ``.xlsm`` → pandas ``read_excel`` with calamine
    - ``.xls``              → pandas ``read_excel`` with xlrd
    - ``.ods``              → pandas ``read_excel`` with the odf engine
      (requires ``odfpy`` — optional runtime dep)
    - ``.csv``              → pandas ``read_csv`` with auto-detected separator
    - ``.tsv``              → pandas ``read_csv`` with ``\\t``
    - Anything else         → ``(None, "Unsupported spreadsheet format: .<ext>")``

    ``sheet_name`` is only used for the Excel/ODS branches; it is silently
    ignored for CSV/TSV (pandas ``read_csv`` has no such concept).

    Returns:
        ``(DataFrame, None)`` on success, ``(None, error_message)`` on any
        download or parse failure. Never raises.
    """
    import pandas as pd

    content, err = _onedrive_download_bytes(item_path, headers)
    if err is not None or content is None:
        return None, err

    ext = os.path.splitext(item_path or "")[1].lower()
    if not ext:
        return None, (
            f"Unsupported spreadsheet format: '{item_path}' has no file "
            "extension. Expected one of: "
            f"{', '.join(sorted(_SPREADSHEET_ALL_EXTS))}."
        )
    if ext not in _SPREADSHEET_ALL_EXTS:
        return None, (
            f"Unsupported spreadsheet format: '{ext}'. Expected one of: "
            f"{', '.join(sorted(_SPREADSHEET_ALL_EXTS))}."
        )

    try:
        if ext in _SPREADSHEET_CSV_EXTS:
            # sep=None + engine='python' lets pandas auto-detect the delimiter
            # (comma, semicolon, pipe). sheet_name is intentionally dropped.
            return (
                pd.read_csv(io.BytesIO(content), sep=None, engine="python"),
                None,
            )
        if ext in _SPREADSHEET_TSV_EXTS:
            return pd.read_csv(io.BytesIO(content), sep="\t"), None

        excel_kwargs: dict = {}
        if ext in _SPREADSHEET_EXCEL_EXTS:
            excel_kwargs["engine"] = "calamine"
        elif ext in _SPREADSHEET_LEGACY_XLS:
            excel_kwargs["engine"] = "xlrd"
        elif ext in _SPREADSHEET_ODS_EXTS:
            excel_kwargs["engine"] = "odf"
        if sheet_name is not None:
            excel_kwargs["sheet_name"] = sheet_name
        return pd.read_excel(io.BytesIO(content), **excel_kwargs), None
    except ImportError as exc:
        return None, (
            f"Cannot parse '{ext}' files: missing optional dependency "
            f"({exc}). Install the appropriate reader (e.g. 'odfpy' for "
            ".ods)."
        )
    except Exception as exc:  # noqa: BLE001 — surface any parser error to caller
        return None, f"Failed to parse spreadsheet ({ext}): {exc}"


def shared_upload_df(
    item_path: str, df: "pd.DataFrame", headers: dict,
) -> str | dict | None:
    """Serialize a DataFrame and upload it back to OneDrive.

    Handles both .xlsx and .csv based on the file extension.
    Retries up to 5 times on HTTP 423 (file locked by OneDrive sync),
    with increasing delays totaling ~63 seconds. OneDrive sync locks
    can persist for 30-60 seconds after a file is closed.

    Args:
        item_path: Path relative to drive root.
        df: DataFrame to serialize and upload.
        headers: Graph API auth headers.

    Returns:
        ``None`` on success. On failure returns either a structured
        ``integration_status`` dict (e.g. when a residual 423 after the
        full retry budget proves the block is tenant-level, not
        file-sync) or a legacy plain-string error for other 4xx/5xx
        cases. Callers must handle both shapes — see ``isinstance(...,
        dict)`` patches in ``campaign_tracker`` / ``followup_tracker``.
    """
    import time as _time
    from urllib.parse import quote as _quote

    raw = (item_path or "").strip()
    if raw.startswith("/drive/root:"):
        raw = raw[len("/drive/root:"):]
    clean = _quote(raw.strip("/"), safe="/")
    url = f"{_GRAPH_BASE}/me/drive/root:/{clean}:/content"

    ext = os.path.splitext(item_path)[1].lower()
    buf = io.BytesIO()
    if ext == ".csv":
        df.to_csv(buf, index=False)
    else:
        df.to_excel(buf, index=False, engine="openpyxl")
    buf.seek(0)
    data = buf.read()

    content_headers = {**headers, "Content-Type": "application/octet-stream"}
    resp = httpx.put(url, content=data, headers=content_headers, timeout=180)

    if resp.status_code == 423:
        for delay in [3, 5, 10, 15, 30]:
            logger.warning("File locked (423), retrying in %ds...", delay)
            _time.sleep(delay)
            resp = httpx.put(url, content=data, headers=content_headers, timeout=180)
            if resp.status_code != 423:
                break

    if resp.status_code in (200, 201):
        return None

    # A residual 423 here means the full retry budget could not unstick
    # the upload — the block is tenant-level, not file-sync. Route
    # through ``_handle_graph_error`` so the structured payload
    # (INTEGRATION_BLOCKED_BY_TENANT) reaches the caller and ultimately
    # the LLM, instead of a generic "Upload failed" string the LLM
    # cannot act on.
    structured = _handle_graph_error(resp, "shared_upload_df")
    if structured is not None and "code" in structured:
        return structured
    return f"Upload failed (HTTP {resp.status_code}): {resp.text[:200]}"


def shared_ensure_col(df: "pd.DataFrame", col: str) -> None:
    """Ensure a column exists in a DataFrame with object dtype.

    Safe to call multiple times — no-ops if column already exists and
    is already object dtype.

    Args:
        df: The DataFrame to modify in place.
        col: Column name to ensure exists.
    """
    if col not in df.columns:
        df[col] = None
    if df[col].dtype != object:
        df[col] = df[col].astype(object)


def shared_graph_headers() -> dict[str, str]:
    """Return Graph API headers for Microsoft Graph calls.

    Token resolution is handled per-invoker inside _graph_headers().
    Used by campaign_tracker and followup_tracker.
    """
    return _graph_headers()


def shared_tracker_key(prefix: str, item_path: str) -> str:
    """Build a tracker cache key scoped to the current AGENT_OWNER.

    Args:
        prefix: Tracker type prefix (e.g. "campaign", "followup", "reply").
        item_path: Path to the file being tracked.
    """
    from apowerb.core.invocation_context import get_agent_owner
    owner = get_agent_owner("default")
    return f"{prefix}::{owner}::{item_path}"


def shared_cell_value(df: "pd.DataFrame", row_idx: int, col: str) -> str:
    """Safely read a cell value as a stripped string, or "" if missing/NaN.

    Args:
        df: The DataFrame.
        row_idx: 0-based row index.
        col: Column name.
    """
    import pandas as pd

    if col in df.columns:
        v = df.at[row_idx, col]
        if pd.notna(v):
            return str(v).strip()
    return ""


def shared_evict_tracker(tracker: dict, max_entries: int = 100) -> None:
    """Evict oldest entries if a tracker dict exceeds max_entries (FIFO)."""
    while len(tracker) > max_entries:
        tracker.pop(next(iter(tracker)))

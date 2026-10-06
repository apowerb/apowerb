"""SharePoint tools — find sites, browse and search document libraries, read
files (text, CSV, Excel, Word, PDF).

Uses the per-user Microsoft integration already wired for SharePoint
(provider ``microsoft_sharepoint``, scope ``Sites.Read.All``, connected from the
Integrations screen) through :mod:`microsoft_auth`. Read-only.

API contract
------------
Verified against the Microsoft Graph v1.0 reference (learn.microsoft.com,
updated 2026-06-19), consulted 2026-10-06:

* ``GET /sites?search={query}`` — least-privileged delegated permission
  ``Sites.Read.All``; returns ``value[]`` of sites (``id``, ``name``,
  ``displayName``, ``webUrl``).
* ``GET /sites/{site-id}/drives`` — the site's document libraries.
* ``GET /drives/{drive-id}/root/children``,
  ``GET /drives/{drive-id}/root:/{path}:/children`` and
  ``GET /drives/{drive-id}/items/{item-id}/children`` — folder listing.
* ``GET /sites/{site-id}/drive/root/search(q='{text}')`` and
  ``GET /drives/{drive-id}/root/search(q='{text}')`` — ``Sites.Read.All`` is
  accepted; paged with ``@odata.nextLink``.
* ``GET /drives/{drive-id}/items/{item-id}`` then ``.../content`` (302 to a
  download URL).
"""

from __future__ import annotations

import io
import os
import re
from logging import getLogger
from typing import Any
from urllib.parse import quote

import httpx

from apowerb.tools_store.portfolio.integration_status import IntegrationStatusError
from apowerb.tools_store.portfolio.microsoft_auth import microsoft_auth_headers
from apowerb.tools_store.portfolio.onedrive_core import _format_item

logger = getLogger(__name__)

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_SCOPE = "offline_access Sites.Read.All"
_HTTP_TIMEOUT_S = 30
_DOWNLOAD_TIMEOUT_S = 60
_MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
_MAX_READ_CHARS = 20_000
_MAX_TOP = 200
# Graph IDs: sites are "host,guid,guid"; drives "b!..."; items alphanumeric+!.
_GRAPH_ID_RE = re.compile(r"^[A-Za-z0-9!._,:-]+$")
_TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".csv",
    ".tsv",
    ".json",
    ".xml",
    ".yaml",
    ".yml",
    ".log",
}


def _headers() -> dict[str, str]:
    return microsoft_auth_headers(
        "SHAREPOINT", scope=_SCOPE, service_label="SharePoint"
    )


def _graph_id(value: str | None, name: str) -> str:
    if not isinstance(value, str) or not _GRAPH_ID_RE.match(value.strip()):
        raise ValueError(f"`{name}` is not a valid Microsoft Graph ID.")
    return value.strip()


def _get(path: str, params: dict | None = None) -> dict:
    resp = httpx.get(
        f"{_GRAPH_BASE}{path}",
        headers=_headers(),
        params=params,
        timeout=_HTTP_TIMEOUT_S,
    )
    resp.raise_for_status()
    return resp.json()


def _error_result(exc: Exception) -> dict:
    if isinstance(exc, IntegrationStatusError):
        return exc.as_tool_result()
    if isinstance(exc, ValueError):
        return {"status": "error", "message": str(exc), "retry": False}
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        hints = {
            401: "Authentication expired. The user needs to reconnect SharePoint.",
            403: "Permission denied: the user cannot access this site or file.",
            404: "Not found: check the site, library or item ID.",
            423: (
                "SharePoint access is blocked by the organization's policy; an "
                "administrator must allow this app in the SharePoint admin center."
            ),
        }
        logger.warning("[SHAREPOINT] HTTP %s: %s", code, exc.response.text[:300])
        return {
            "status": "error",
            "http_status": code,
            "message": f"{hints.get(code, 'SharePoint request failed.')} (HTTP {code})",
            "retry": code == 429 or code >= 500,
        }
    if isinstance(exc, httpx.TimeoutException):
        return {
            "status": "error",
            "message": "SharePoint request timed out.",
            "retry": True,
        }
    logger.warning("[SHAREPOINT] request failed: %s", exc)
    return {
        "status": "error",
        "message": f"SharePoint request failed: {exc}",
        "retry": False,
    }


def _items(data: dict, limit: int) -> list[dict]:
    items = []
    for item in (data.get("value") or [])[:limit]:
        summary = _format_item(item)
        summary["driveId"] = (item.get("parentReference") or {}).get("driveId")
        items.append(summary)
    return items


def _top(limit: int) -> int:
    return max(1, min(limit, _MAX_TOP))


def tool_sharepoint_search_sites(query: str, limit: int = 20) -> dict:
    """Find SharePoint sites the user can access.

    Args:
        query (str): Keywords matched against site names and descriptions.
            Required.
        limit (int): Maximum sites to return (1–200). Default: 20.

    Returns:
        dict: On success, ``status`` "success" with ``count`` and ``sites``
        (``id``, ``name``, ``webUrl``, ``description``). Pass a site ``id`` to
        the other SharePoint tools.
    """
    if not isinstance(query, str) or not query.strip():
        return {"status": "error", "message": "`query` is required.", "retry": False}
    try:
        data = _get("/sites", params={"search": query.strip()})
    except Exception as exc:  # noqa: BLE001 — mapped to a structured error dict
        return _error_result(exc)
    sites = [
        {
            "id": s.get("id"),
            "name": s.get("displayName") or s.get("name"),
            "webUrl": s.get("webUrl"),
            "description": s.get("description"),
        }
        for s in (data.get("value") or [])[: _top(limit)]
    ]
    return {"status": "success", "count": len(sites), "sites": sites}


def tool_sharepoint_list_libraries(site_id: str) -> dict:
    """List the document libraries (drives) of a SharePoint site.

    Args:
        site_id (str): Site ID from ``tool_sharepoint_search_sites``. Required.

    Returns:
        dict: On success, ``status`` "success" with ``libraries`` (``id``,
        ``name``, ``webUrl``).
    """
    try:
        data = _get(f"/sites/{_graph_id(site_id, 'site_id')}/drives")
    except Exception as exc:  # noqa: BLE001 — mapped to a structured error dict
        return _error_result(exc)
    libraries = [
        {"id": d.get("id"), "name": d.get("name"), "webUrl": d.get("webUrl")}
        for d in data.get("value") or []
    ]
    return {"status": "success", "count": len(libraries), "libraries": libraries}


def tool_sharepoint_list_files(
    drive_id: str,
    folder_path: str | None = None,
    folder_id: str | None = None,
    limit: int = 50,
) -> dict:
    """List files and folders in a SharePoint document library.

    Args:
        drive_id (str): Library ID from ``tool_sharepoint_list_libraries``.
            Required.
        folder_path (str): Folder path inside the library, e.g.
            "Contrats/2026". Optional — the library root otherwise.
        folder_id (str): Folder item ID (takes precedence over
            ``folder_path``). Optional.
        limit (int): Maximum entries to return (1–200). Default: 50.

    Returns:
        dict: On success, ``status`` "success" with ``count`` and ``items``
        (``id``, ``name``, ``type``, ``size``, ``lastModified``, ``webUrl``,
        ``driveId``…).
    """
    try:
        drive = _graph_id(drive_id, "drive_id")
        if folder_id:
            path = f"/drives/{drive}/items/{_graph_id(folder_id, 'folder_id')}/children"
        elif folder_path and folder_path.strip("/ "):
            clean = quote(folder_path.strip("/ "), safe="/")
            path = f"/drives/{drive}/root:/{clean}:/children"
        else:
            path = f"/drives/{drive}/root/children"
        data = _get(path, params={"$top": _top(limit)})
    except Exception as exc:  # noqa: BLE001 — mapped to a structured error dict
        return _error_result(exc)
    items = _items(data, _top(limit))
    return {
        "status": "success",
        "count": len(items),
        "has_more": bool(data.get("@odata.nextLink")),
        "items": items,
    }


def tool_sharepoint_search_files(
    query: str,
    site_id: str | None = None,
    drive_id: str | None = None,
    limit: int = 25,
) -> dict:
    """Search files by name, metadata and content in a SharePoint site or
    library.

    Args:
        query (str): Search text. Required.
        site_id (str): Search the site's default library. One of ``site_id``
            or ``drive_id`` is required.
        drive_id (str): Search this library (takes precedence).
        limit (int): Maximum results (1–200). Default: 25.

    Returns:
        dict: On success, ``status`` "success" with ``count`` and ``items``.
    """
    if not isinstance(query, str) or not query.strip():
        return {"status": "error", "message": "`query` is required.", "retry": False}
    try:
        if drive_id:
            root = f"/drives/{_graph_id(drive_id, 'drive_id')}/root"
        elif site_id:
            root = f"/sites/{_graph_id(site_id, 'site_id')}/drive/root"
        else:
            raise ValueError("Provide `site_id` or `drive_id`.")
        # OData string literal: single quotes are escaped by doubling them.
        q = quote(query.strip().replace("'", "''"), safe="")
        data = _get(f"{root}/search(q='{q}')", params={"$top": _top(limit)})
    except Exception as exc:  # noqa: BLE001 — mapped to a structured error dict
        return _error_result(exc)
    items = _items(data, _top(limit))
    return {"status": "success", "count": len(items), "items": items}


def _extract_text(name: str, content: bytes) -> str | None:
    """Text of a downloaded file, or None when the format is not supported."""
    ext = os.path.splitext(name)[1].lower()
    if ext in _TEXT_EXTENSIONS:
        return content.decode("utf-8", errors="replace")
    if ext in (".xlsx", ".xlsm", ".xls"):
        import pandas as pd

        engine = "calamine" if ext != ".xls" else None
        sheets = pd.read_excel(io.BytesIO(content), sheet_name=None, engine=engine)
        return "\n\n".join(
            f"## {sheet}\n{frame.to_csv(index=False)}"
            for sheet, frame in sheets.items()
        )
    if ext == ".docx":
        import docx

        document = docx.Document(io.BytesIO(content))
        return "\n".join(p.text for p in document.paragraphs)
    if ext == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    return None


def tool_sharepoint_read_file(drive_id: str, item_id: str) -> dict:
    """Read a SharePoint file as text (txt, md, csv, json, xlsx, docx, pdf…).

    Args:
        drive_id (str): Library ID (``driveId`` of a listed or found item).
            Required.
        item_id (str): The file's item ID. Required.

    Returns:
        dict: On success, ``status`` "success" with ``name``, ``webUrl``,
        ``content`` (text, up to 20,000 characters) and ``truncated``. Other
        formats return metadata and ``webUrl`` only.
    """
    try:
        drive = _graph_id(drive_id, "drive_id")
        item = _graph_id(item_id, "item_id")
        meta = _get(f"/drives/{drive}/items/{item}")
        if "folder" in meta:
            raise ValueError("This item is a folder; use tool_sharepoint_list_files.")
        name = meta.get("name") or ""
        summary = _format_item(meta)
        if (meta.get("size") or 0) > _MAX_DOWNLOAD_BYTES:
            return {
                "status": "success",
                **summary,
                "content": None,
                "message": "File too large to read inline; open it with webUrl.",
            }
        resp = httpx.get(
            f"{_GRAPH_BASE}/drives/{drive}/items/{item}/content",
            headers=_headers(),
            timeout=_DOWNLOAD_TIMEOUT_S,
            follow_redirects=True,
        )
        resp.raise_for_status()
        text = _extract_text(name, resp.content)
    except Exception as exc:  # noqa: BLE001 — mapped to a structured error dict
        return _error_result(exc)

    if text is None:
        return {
            "status": "success",
            **summary,
            "content": None,
            "message": "This file type cannot be read as text; open it with webUrl.",
        }
    result: dict[str, Any] = {
        "status": "success",
        **summary,
        "content": text[:_MAX_READ_CHARS],
        "truncated": len(text) > _MAX_READ_CHARS,
    }
    return result

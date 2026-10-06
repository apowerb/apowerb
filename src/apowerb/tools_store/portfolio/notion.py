"""Notion tools — search, read pages, query databases and create pages.

Lets an agent use the user's Notion workspace as a data source: find pages and
databases, read a page as plain text, query a database, and create a page.

API contract
------------
Verified against the Notion API reference (developers.notion.com), consulted
2026-10-06:

* Base URL: ``https://api.notion.com/v1``.
* Auth: an internal integration token in ``Authorization: Bearer <token>``,
  plus a ``Notion-Version`` header. Pinned to ``2026-03-11`` (latest).
* Since ``2025-09-03`` a database holds one or more *data sources*:
  ``GET /databases/{id}`` returns ``data_sources: [{id, name}]``; queries go to
  ``POST /data_sources/{id}/query`` (``filter``, ``sorts``, ``page_size`` ≤ 100,
  ``start_cursor``); pages are created with
  ``parent: {"type": "data_source_id", ...}``; the search filter value is
  ``"page"`` or ``"data_source"``.
* Search: ``POST /search`` — ``query``, ``filter``, ``page_size``.
* Page content: ``GET /blocks/{page_id}/children`` (paginated, ≤ 100/page).
* Create page: ``POST /pages`` — ``parent``, ``properties``, ``children``
  (≤ 100 blocks, rich text ≤ 2000 characters each).
* List responses: ``{"results", "has_more", "next_cursor"}``.
* Errors: ``{"object": "error", "status", "code", "message"}``.

The integration only sees pages and databases explicitly shared with it in
Notion ("Connections" menu of the page).

``NOTION_API_TOKEN`` is read at module level so the ToolsStore parameter scanner
surfaces it as a UI setting; it is also read at call time so a token configured
through the UI is honoured.
"""

from __future__ import annotations

import os
import re
from logging import getLogger
from typing import Any
from urllib.parse import urlsplit

logger = getLogger(__name__)

# Read at module level so the ToolsStore scanner (regex on os.getenv) discovers
# it for the UI. Re-read at call time in _token().
_NOTION_API_TOKEN = os.getenv("NOTION_API_TOKEN", "")

_BASE_URL = "https://api.notion.com/v1"
_NOTION_VERSION = "2026-03-11"
_HTTP_TIMEOUT_S = 30
_PAGE_SIZE_MAX = 100  # Notion's documented per-request maximum.
_RICH_TEXT_MAX = 2000  # Max characters in one rich text object.
_READ_MAX_BLOCKS = 1000

_ID_RE = re.compile(
    r"([0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12})", re.I
)


def _token() -> str:
    """Return the Notion integration token, read at call time, or raise."""
    token = os.environ.get("NOTION_API_TOKEN") or ""
    if not token:
        raise EnvironmentError("NOTION_API_TOKEN not set")
    return token


def _notion_id(value: str | None, name: str) -> str:
    """Extract a Notion ID from a bare ID or a Notion URL, or raise ValueError."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"`{name}` is required.")
    # In a URL the ID is the last 32-hex run of the path (after the title
    # slug); the query is ignored — database URLs carry ``?v=<view id>``.
    matches = _ID_RE.findall(urlsplit(value.strip()).path)
    if not matches:
        raise ValueError(f"`{name}` is not a valid Notion ID or URL: {value!r}")
    return matches[-1].replace("-", "").lower()


def _request(
    method: str,
    path: str,
    *,
    json_body: dict | None = None,
    params: dict | None = None,
) -> dict:
    """Call the Notion API and return the parsed JSON.

    Raises EnvironmentError (missing token) or propagates httpx errors.
    """
    import httpx

    token = _token()
    resp = httpx.request(
        method,
        f"{_BASE_URL}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": _NOTION_VERSION,
            "Content-Type": "application/json",
        },
        json=json_body,
        params=params,
        timeout=_HTTP_TIMEOUT_S,
    )
    resp.raise_for_status()
    return resp.json()


def _error_result(exc: Exception) -> dict:
    """Map an exception to the tool's error dict, keeping HTTP status if any."""
    import httpx

    if isinstance(exc, (EnvironmentError, ValueError)):
        return {"status": "error", "error_message": str(exc)}
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        detail = exc.response.text[:500]
        if code == 401:
            detail = f"Authentication failed (check NOTION_API_TOKEN). {detail}"
        elif code == 404:
            detail = (
                "Not found — make sure the page or database is shared with the "
                f"integration (Connections menu in Notion). {detail}"
            )
        logger.warning("[NOTION] HTTP %s: %s", code, detail)
        return {
            "status": "error",
            "http_status": code,
            "error_message": f"Notion error (HTTP {code}). {detail}",
        }
    logger.warning("[NOTION] request failed: %s", exc)
    return {"status": "error", "error_message": f"Request to Notion failed: {exc}"}


def _plain_text(rich_text: list | None) -> str:
    return "".join(part.get("plain_text", "") for part in rich_text or [])


def _title_of(obj: dict) -> str:
    """Title of a page or data source object."""
    if isinstance(obj.get("title"), list):  # data source / database
        return _plain_text(obj["title"])
    for prop in (obj.get("properties") or {}).values():
        if isinstance(prop, dict) and prop.get("type") == "title":
            return _plain_text(prop.get("title"))
    return ""


def _property_value(prop: dict) -> Any:
    """Flatten one page property to a JSON-friendly scalar or list."""
    ptype = prop.get("type")
    value = prop.get(ptype)
    if ptype in ("title", "rich_text"):
        return _plain_text(value)
    if ptype in ("select", "status"):
        return (value or {}).get("name")
    if ptype == "multi_select":
        return [opt.get("name") for opt in value or []]
    if ptype == "date":
        return value  # {"start", "end", "time_zone"} or None
    if ptype == "people":
        return [p.get("name") or p.get("id") for p in value or []]
    if ptype == "relation":
        return [r.get("id") for r in value or []]
    if ptype == "files":
        return [f.get("name") for f in value or []]
    if ptype == "formula":
        return (value or {}).get((value or {}).get("type"))
    if ptype == "unique_id" and value:
        prefix = value.get("prefix")
        return f"{prefix}-{value.get('number')}" if prefix else value.get("number")
    return value  # number, checkbox, url, email, phone_number, *_time, ...


def _summarize(obj: dict) -> dict:
    summary = {
        "id": obj.get("id"),
        "object": obj.get("object"),
        "title": _title_of(obj),
        "url": obj.get("url"),
        "last_edited_time": obj.get("last_edited_time"),
    }
    if obj.get("object") == "page":
        summary["properties"] = {
            name: _property_value(prop)
            for name, prop in (obj.get("properties") or {}).items()
            if isinstance(prop, dict)
        }
    return summary


def _block_text(block: dict) -> str:
    """Render one block as a line of plain text (Markdown-ish prefixes)."""
    btype = block.get("type", "")
    data = block.get(btype) or {}
    text = _plain_text(data.get("rich_text"))
    if btype.startswith("heading_"):
        return f"{'#' * int(btype[-1])} {text}"
    if btype == "bulleted_list_item":
        return f"- {text}"
    if btype == "numbered_list_item":
        return f"1. {text}"
    if btype == "to_do":
        return f"[{'x' if data.get('checked') else ' '}] {text}"
    if btype == "quote":
        return f"> {text}"
    if btype == "code":
        return f"```{data.get('language', '')}\n{text}\n```"
    if btype == "child_page":
        return f"[page: {data.get('title', '')}]"
    if btype == "child_database":
        return f"[database: {data.get('title', '')}]"
    if btype == "divider":
        return "---"
    return text


def tool_notion_search(
    query: str | None = None,
    object_type: str | None = None,
    limit: int = 10,
) -> dict:
    """Search pages and databases shared with the Notion integration.

    Args:
        query (str): Text to match against titles. Optional — omit to list
            everything the integration can see.
        object_type (str): "page" or "database" to restrict results. Optional.
        limit (int): Maximum results to return (1–100). Default: 10.

    Returns:
        dict: On success, ``status`` "success" with ``count``, ``has_more`` and
        ``results`` (``id``, ``object``, ``title``, ``url``…). Database hits are
        data sources: pass their ``id`` as ``data_source_id`` to
        ``tool_notion_query_database``. On failure, ``status`` "error" with
        ``error_message``.
    """
    body: dict[str, Any] = {"page_size": max(1, min(limit, _PAGE_SIZE_MAX))}
    if query and query.strip():
        body["query"] = query.strip()
    if object_type:
        kind = object_type.strip().lower()
        if kind in ("database", "data_source"):
            kind = "data_source"
        elif kind != "page":
            return {
                "status": "error",
                "error_message": "`object_type` must be 'page' or 'database'.",
            }
        body["filter"] = {"property": "object", "value": kind}

    try:
        data = _request("POST", "/search", json_body=body)
    except Exception as exc:  # noqa: BLE001 — mapped to a structured error dict
        return _error_result(exc)

    results = [_summarize(obj) for obj in data.get("results") or []]
    return {
        "status": "success",
        "count": len(results),
        "has_more": bool(data.get("has_more")),
        "results": results,
    }


def tool_notion_read_page(page_id: str, max_blocks: int = 300) -> dict:
    """Read a Notion page: its properties and its content as plain text.

    Only top-level blocks are read; nested pages and databases appear as
    ``[page: …]`` / ``[database: …]`` markers.

    Args:
        page_id (str): The page ID or its Notion URL. Required.
        max_blocks (int): Maximum number of blocks to read (1–1000).
            Default: 300.

    Returns:
        dict: On success, ``status`` "success" with ``id``, ``title``, ``url``,
        ``properties``, ``content`` (text) and ``truncated``. On failure,
        ``status`` "error" with ``error_message``.
    """
    try:
        pid = _notion_id(page_id, "page_id")
        page = _request("GET", f"/pages/{pid}")
        cap = max(1, min(max_blocks, _READ_MAX_BLOCKS))
        lines: list[str] = []
        cursor: str | None = None
        truncated = False
        while True:
            params: dict[str, Any] = {"page_size": _PAGE_SIZE_MAX}
            if cursor:
                params["start_cursor"] = cursor
            chunk = _request("GET", f"/blocks/{pid}/children", params=params)
            for block in chunk.get("results") or []:
                if len(lines) >= cap:
                    truncated = True
                    break
                lines.append(_block_text(block))
            cursor = chunk.get("next_cursor")
            if truncated or not chunk.get("has_more") or not cursor:
                truncated = truncated or bool(chunk.get("has_more"))
                break
    except Exception as exc:  # noqa: BLE001 — mapped to a structured error dict
        return _error_result(exc)

    summary = _summarize(page)
    return {
        "status": "success",
        "id": summary["id"],
        "title": summary["title"],
        "url": summary["url"],
        "properties": summary.get("properties", {}),
        "content": "\n".join(lines),
        "truncated": truncated,
    }


def _resolve_data_source(database_id: str | None, data_source_id: str | None) -> str:
    """Return a data source ID, looking it up from the database if needed."""
    if data_source_id:
        return _notion_id(data_source_id, "data_source_id")
    dbid = _notion_id(database_id, "database_id")
    database = _request("GET", f"/databases/{dbid}")
    sources = database.get("data_sources") or []
    if len(sources) == 1:
        return sources[0]["id"]
    if not sources:
        raise ValueError("This database has no data source.")
    names = ", ".join(f"{s.get('name')!r} ({s.get('id')})" for s in sources)
    raise ValueError(
        f"This database has several data sources; pass `data_source_id`: {names}"
    )


def tool_notion_query_database(
    database_id: str | None = None,
    data_source_id: str | None = None,
    filter: dict | None = None,
    sorts: list | None = None,
    limit: int = 50,
) -> dict:
    """Query the rows (pages) of a Notion database.

    Args:
        database_id (str): Database ID or URL. Required unless
            ``data_source_id`` is given.
        data_source_id (str): Data source ID (as returned by
            ``tool_notion_search``). Takes precedence over ``database_id``.
        filter (dict): A Notion filter object, e.g.
            ``{"property": "Status", "status": {"equals": "Done"}}``. Optional.
        sorts (list): Notion sort objects, e.g.
            ``[{"property": "Date", "direction": "descending"}]``. Optional.
        limit (int): Maximum rows to return (1–1000). Default: 50.

    Returns:
        dict: On success, ``status`` "success" with ``count``, ``has_more``
        and ``results`` (each row with ``id``, ``title``, ``url`` and flattened
        ``properties``). On failure, ``status`` "error" with ``error_message``.
    """
    cap = max(1, min(limit, _READ_MAX_BLOCKS))
    try:
        dsid = _resolve_data_source(database_id, data_source_id)
        rows: list[dict] = []
        cursor: str | None = None
        has_more = False
        while len(rows) < cap:
            body: dict[str, Any] = {"page_size": min(_PAGE_SIZE_MAX, cap - len(rows))}
            if filter:
                body["filter"] = filter
            if sorts:
                body["sorts"] = sorts
            if cursor:
                body["start_cursor"] = cursor
            data = _request("POST", f"/data_sources/{dsid}/query", json_body=body)
            rows.extend(_summarize(obj) for obj in data.get("results") or [])
            has_more = bool(data.get("has_more"))
            cursor = data.get("next_cursor")
            if not has_more or not cursor:
                break
    except Exception as exc:  # noqa: BLE001 — mapped to a structured error dict
        return _error_result(exc)

    rows = rows[:cap]
    return {
        "status": "success",
        "count": len(rows),
        "has_more": has_more,
        "results": rows,
    }


def _paragraphs(content: str) -> list[dict]:
    """Split text on blank lines into paragraph blocks within Notion limits."""
    blocks: list[dict] = []
    for para in re.split(r"\n\s*\n", content.strip()):
        para = para.strip()
        for start in range(0, len(para), _RICH_TEXT_MAX):
            blocks.append(
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [
                            {
                                "type": "text",
                                "text": {
                                    "content": para[start : start + _RICH_TEXT_MAX]
                                },
                            }
                        ]
                    },
                }
            )
    return blocks


def tool_notion_create_page(
    title: str,
    parent_page_id: str | None = None,
    database_id: str | None = None,
    data_source_id: str | None = None,
    content: str | None = None,
    properties: dict | None = None,
) -> dict:
    """Create a Notion page under a page or as a new row of a database.

    Give exactly one parent: ``parent_page_id``, ``database_id`` or
    ``data_source_id``.

    Args:
        title (str): The page title. Required.
        parent_page_id (str): ID or URL of the parent page. Optional.
        database_id (str): ID or URL of the database to add a row to. Optional.
        data_source_id (str): Data source to add a row to. Optional.
        content (str): Page body as plain text; blank lines separate
            paragraphs. Optional.
        properties (dict): Extra Notion property values (database rows only),
            in Notion's format, e.g. ``{"Status": {"status": {"name": "Todo"}}}``.
            Optional.

    Returns:
        dict: On success, ``status`` "success" with ``id`` and ``url``. On
        failure, ``status`` "error" with ``error_message``.
    """
    if not isinstance(title, str) or not title.strip():
        return {"status": "error", "error_message": "`title` is required."}
    parents = [p for p in (parent_page_id, database_id, data_source_id) if p]
    if len(parents) != 1:
        return {
            "status": "error",
            "error_message": (
                "Provide exactly one of `parent_page_id`, `database_id` or "
                "`data_source_id`."
            ),
        }
    title_text = [{"type": "text", "text": {"content": title.strip()[:_RICH_TEXT_MAX]}}]
    children = _paragraphs(content) if content and content.strip() else []
    if len(children) > _PAGE_SIZE_MAX:
        return {
            "status": "error",
            "error_message": f"`content` is too long (max {_PAGE_SIZE_MAX} paragraphs).",
        }

    try:
        if parent_page_id:
            if properties:
                raise ValueError("`properties` only apply to database rows.")
            parent = {
                "type": "page_id",
                "page_id": _notion_id(parent_page_id, "parent_page_id"),
            }
            props: dict[str, Any] = {"title": {"title": title_text}}
        else:
            dsid = _resolve_data_source(database_id, data_source_id)
            schema = _request("GET", f"/data_sources/{dsid}")
            title_prop = next(
                (
                    name
                    for name, prop in (schema.get("properties") or {}).items()
                    if isinstance(prop, dict) and prop.get("type") == "title"
                ),
                None,
            )
            if not title_prop:
                raise ValueError("No title property found in this database.")
            parent = {"type": "data_source_id", "data_source_id": dsid}
            props = dict(properties or {})
            props[title_prop] = {"title": title_text}

        body: dict[str, Any] = {"parent": parent, "properties": props}
        if children:
            body["children"] = children
        data = _request("POST", "/pages", json_body=body)
    except Exception as exc:  # noqa: BLE001 — mapped to a structured error dict
        return _error_result(exc)

    return {"status": "success", "id": data.get("id"), "url": data.get("url")}

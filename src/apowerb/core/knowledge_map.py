"""Knowledge map helpers — read/write .knowledge_map.json for RAG indexation tracking.

Each agent (or session) has a JSON file at
``uploads/{scope}/.knowledge_map.json`` where *scope* is either a
``session_id`` (per-session knowledge) or ``agent_id`` (per-agent, legacy).
The file records every indexed source (file, URL, DB export, S3 object) along
with its RAG knowledge_id and processing status.
"""

import json
import os
import threading
from datetime import datetime, timezone
from logging import getLogger
from apowerb.configs.paths import uploads_dir

logger = getLogger(__name__)

# ---------------------------------------------------------------------------
# S2a — Per-scope locks to prevent race conditions on the JSON file.
# Uses threading.Lock because these functions run inside threads via
# asyncio.to_thread() from the router layer.
# The *scope* is either a session_id (per-session KB) or agent_id (legacy).
# ---------------------------------------------------------------------------

_locks: dict[str, threading.Lock] = {}
_locks_lock = threading.Lock()


def _resolve_scope(agent_id: str, session_id: str | None) -> str:
    """Return the effective scope key — *session_id* if provided, else *agent_id*."""
    return session_id if session_id else agent_id


def _get_lock(scope: str) -> threading.Lock:
    """Return (or create) a threading.Lock for *scope*."""
    with _locks_lock:
        if scope not in _locks:
            _locks[scope] = threading.Lock()
        return _locks[scope]


def _map_path(scope: str) -> str:
    """Return the path to the knowledge map file for the given *scope*."""
    return str(uploads_dir() / scope / ".knowledge_map.json")


def read_knowledge_map(agent_id: str, *, session_id: str | None = None) -> dict:
    """Read the knowledge map for *agent_id* (or *session_id* if provided).

    Returns ``{"sources": []}`` if the file does not exist or cannot be parsed.
    """
    scope = _resolve_scope(agent_id, session_id)
    path = _map_path(scope)
    if not os.path.exists(path):
        return {"sources": []}

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data
    except Exception as exc:
        logger.warning("[KMAP] Failed to read %s: %s", path, exc)
        return {"sources": []}


def _write_knowledge_map(scope: str, data: dict) -> None:
    """Persist the knowledge map dict to disk, creating directories if needed."""
    path = _map_path(scope)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def append_source(
    agent_id: str,
    source_type: str,
    name: str,
    knowledge_id: str,
    status: str = "processing",
    *,
    session_id: str | None = None,
) -> dict:
    """Append a new source entry to the knowledge map and return it.

    Creates the file if it does not exist yet.
    Thread-safe thanks to per-scope locking (S2a).

    When *session_id* is provided the source is stored under
    ``uploads/{session_id}/`` instead of ``uploads/{agent_id}/``.
    """
    scope = _resolve_scope(agent_id, session_id)
    lock = _get_lock(scope)
    with lock:
        kmap = read_knowledge_map(agent_id, session_id=session_id)

        source = {
            "type": source_type,
            "name": name,
            "knowledge_id": str(knowledge_id),
            "status": status,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        kmap.setdefault("sources", []).append(source)
        _write_knowledge_map(scope, kmap)

    logger.info(
        "[KMAP] Appended source type=%s name=%r kid=%s scope=%s (agent=%s)",
        source_type, name, knowledge_id, scope, agent_id,
    )
    return source


def update_status(
    agent_id: str,
    knowledge_id: str,
    status: str,
    *,
    session_id: str | None = None,
) -> bool:
    """Update the status of a source identified by *knowledge_id*.

    Returns ``True`` if the source was found and updated, ``False`` otherwise.
    Thread-safe thanks to per-scope locking (S2a).
    """
    scope = _resolve_scope(agent_id, session_id)
    lock = _get_lock(scope)
    with lock:
        kmap = read_knowledge_map(agent_id, session_id=session_id)
        updated = False

        for source in kmap.get("sources", []):
            if str(source.get("knowledge_id")) == str(knowledge_id):
                source["status"] = status
                updated = True
                break

        if updated:
            _write_knowledge_map(scope, kmap)

    if updated:
        logger.info(
            "[KMAP] Updated kid=%s -> status=%s for scope=%s (agent=%s)",
            knowledge_id, status, scope, agent_id,
        )

    return updated

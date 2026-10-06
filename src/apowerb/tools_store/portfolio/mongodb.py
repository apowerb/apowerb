"""MongoDB tools — list collections, find documents and run aggregations.

Uses PyMongo, checked against pymongo 4.18.2 on 2026-10-06:
``MongoClient(uri, serverSelectionTimeoutMS=, appname=)``,
``Database.list_collection_names()``, ``Collection.find(filter, projection)``
with ``sort`` / ``limit`` / ``max_time_ms``, ``Collection.aggregate(pipeline,
maxTimeMS=)``, ``Collection.count_documents``.

Read-only: no insert/update/delete tool, and aggregation pipelines containing
a write stage (``$out``, ``$merge``) are refused. The connection string should
still use a user with the ``read`` role only.

``MONGODB_*`` settings are read at module level so the ToolsStore parameter
scanner surfaces them in the UI, and again at call time.
"""

from __future__ import annotations

import datetime
import hashlib
import os
import re
import threading
from logging import getLogger
from typing import Any

logger = getLogger(__name__)

# Read at module level so the ToolsStore scanner (regex on os.getenv) discovers
# them for the UI. Re-read at call time.
_MONGODB_URI = os.getenv("MONGODB_URI", "")
_MONGODB_DATABASE = os.getenv("MONGODB_DATABASE", "")

_SERVER_TIMEOUT_MS = 10_000
_QUERY_TIMEOUT_MS = 60_000
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 1000
_WRITE_STAGES = ("$out", "$merge")
_NAME_RE = re.compile(r"^[^\x00$][^\x00]*$")

_client_lock = threading.Lock()
_clients: dict[str, Any] = {}


def _client():
    """Return a MongoClient for MONGODB_URI, reused across calls (it pools)."""
    from pymongo import MongoClient

    uri = (os.environ.get("MONGODB_URI") or "").strip()
    if not uri:
        raise EnvironmentError("MONGODB_URI not set")
    key = hashlib.sha256(uri.encode()).hexdigest()
    with _client_lock:
        client = _clients.get(key)
        if client is None:
            for old in _clients.values():
                old.close()
            _clients.clear()
            client = MongoClient(
                uri, serverSelectionTimeoutMS=_SERVER_TIMEOUT_MS, appname="apowerb"
            )
            _clients[key] = client
        return client


def _database(name: str | None):
    db_name = (name or os.environ.get("MONGODB_DATABASE") or "").strip()
    if not db_name:
        raise ValueError("Pass `database` or set MONGODB_DATABASE.")
    if not _NAME_RE.match(db_name):
        raise ValueError("Invalid `database` name.")
    return _client()[db_name]


def _collection(database: str | None, collection: str):
    if not isinstance(collection, str) or not _NAME_RE.match(collection.strip()):
        raise ValueError("`collection` is required.")
    return _database(database)[collection.strip()]


def _limit(limit: int | None) -> int:
    if not isinstance(limit, int) or limit < 1:
        return _DEFAULT_LIMIT
    return min(limit, _MAX_LIMIT)


def _jsonable(value: Any) -> Any:
    """Convert BSON values (ObjectId, Decimal128, datetime, bytes…) to JSON."""
    from bson import Decimal128, ObjectId

    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, Decimal128):
        return str(value.to_decimal())
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return "<binary>"
    return value


def _contains_write_stage(pipeline: list) -> bool:
    """True if a write stage appears anywhere (also inside $facet/$lookup)."""

    def walk(node: Any) -> bool:
        if isinstance(node, dict):
            return any(k in _WRITE_STAGES or walk(v) for k, v in node.items())
        if isinstance(node, list):
            return any(walk(item) for item in node)
        return False

    return walk(pipeline)


def _error_result(exc: Exception) -> dict:
    if isinstance(exc, (EnvironmentError, ValueError, TypeError)):
        return {"status": "error", "error_message": str(exc)}
    logger.warning("[MONGODB] request failed: %s", exc)
    return {"status": "error", "error_message": f"MongoDB error: {exc}"}


def tool_mongodb_list_collections(database: str | None = None) -> dict:
    """List the collections of a MongoDB database.

    Args:
        database (str): Database name. Defaults to MONGODB_DATABASE.

    Returns:
        dict: On success, ``status`` "success" with ``database`` and
        ``collections``. On failure, ``status`` "error" with ``error_message``.
    """
    try:
        db = _database(database)
        names = sorted(db.list_collection_names())
    except Exception as exc:  # noqa: BLE001 — mapped to a structured error dict
        return _error_result(exc)
    return {"status": "success", "database": db.name, "collections": names}


def tool_mongodb_find(
    collection: str,
    filter: dict | None = None,
    projection: dict | None = None,
    sort: dict | None = None,
    limit: int = 50,
    database: str | None = None,
) -> dict:
    """Find documents in a MongoDB collection.

    Args:
        collection (str): Collection name. Required.
        filter (dict): MongoDB query, e.g. ``{"status": "active",
            "amount": {"$gt": 100}}``. Optional.
        projection (dict): Fields to include/exclude, e.g.
            ``{"name": 1, "_id": 0}``. Optional.
        sort (dict): Sort spec, e.g. ``{"created_at": -1}``. Optional.
        limit (int): Maximum documents (1–1000). Default: 50.
        database (str): Database name. Defaults to MONGODB_DATABASE.

    Returns:
        dict: On success, ``status`` "success" with ``count``, ``truncated``
        and ``documents`` (ObjectId and dates as strings). On failure,
        ``status`` "error" with ``error_message``.
    """
    cap = _limit(limit)
    try:
        if filter is not None and not isinstance(filter, dict):
            raise TypeError("`filter` must be an object.")
        coll = _collection(database, collection)
        cursor = coll.find(filter or {}, projection or None).max_time_ms(
            _QUERY_TIMEOUT_MS
        )
        if sort:
            cursor = cursor.sort(list(sort.items()))
        # One extra document tells whether the result was cut.
        docs = list(cursor.limit(cap + 1))
    except Exception as exc:  # noqa: BLE001 — mapped to a structured error dict
        return _error_result(exc)
    return {
        "status": "success",
        "count": min(len(docs), cap),
        "truncated": len(docs) > cap,
        "documents": [_jsonable(d) for d in docs[:cap]],
    }


def tool_mongodb_aggregate(
    collection: str,
    pipeline: list,
    limit: int = 100,
    database: str | None = None,
) -> dict:
    """Run a read-only aggregation pipeline on a MongoDB collection.

    Write stages (``$out``, ``$merge``) are refused.

    Args:
        collection (str): Collection name. Required.
        pipeline (list): Aggregation stages, e.g. ``[{"$match": {...}},
            {"$group": {"_id": "$country", "total": {"$sum": "$amount"}}}]``.
            Required.
        limit (int): Maximum result documents (1–1000). Default: 100.
        database (str): Database name. Defaults to MONGODB_DATABASE.

    Returns:
        dict: On success, ``status`` "success" with ``count``, ``truncated``
        and ``documents``. On failure, ``status`` "error" with
        ``error_message``.
    """
    cap = _limit(limit)
    try:
        if not isinstance(pipeline, list) or not all(
            isinstance(s, dict) for s in pipeline
        ):
            raise TypeError("`pipeline` must be a list of stage objects.")
        if _contains_write_stage(pipeline):
            raise ValueError("Write stages ($out, $merge) are not allowed.")
        coll = _collection(database, collection)
        stages = [*pipeline, {"$limit": cap + 1}]
        docs = list(coll.aggregate(stages, maxTimeMS=_QUERY_TIMEOUT_MS))
    except Exception as exc:  # noqa: BLE001 — mapped to a structured error dict
        return _error_result(exc)
    return {
        "status": "success",
        "count": min(len(docs), cap),
        "truncated": len(docs) > cap,
        "documents": [_jsonable(d) for d in docs[:cap]],
    }


def tool_mongodb_count(
    collection: str, filter: dict | None = None, database: str | None = None
) -> dict:
    """Count documents matching a filter in a MongoDB collection.

    Args:
        collection (str): Collection name. Required.
        filter (dict): MongoDB query. Optional — all documents otherwise.
        database (str): Database name. Defaults to MONGODB_DATABASE.

    Returns:
        dict: On success, ``status`` "success" with ``count``.
    """
    try:
        if filter is not None and not isinstance(filter, dict):
            raise TypeError("`filter` must be an object.")
        coll = _collection(database, collection)
        count = coll.count_documents(filter or {}, maxTimeMS=_QUERY_TIMEOUT_MS)
    except Exception as exc:  # noqa: BLE001 — mapped to a structured error dict
        return _error_result(exc)
    return {"status": "success", "count": count}

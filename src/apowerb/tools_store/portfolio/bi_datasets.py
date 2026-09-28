"""Accès aux jeux de données importés (BI), limité au propriétaire de l'agent.

Dans le style de ``business_intelligence.py`` : ``_run_async``, ``_get_session``,
``_agent_owner`` sont dupliqués ici plutôt que partagés, pour éviter un
couplage inter-modules (même choix que ``dataset_router._resolve_path_from_id``).
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import re
import threading
from datetime import date, datetime
from logging import getLogger
from typing import Any

logger = getLogger(__name__)

# Message unique, qu'un jeu appartienne à quelqu'un d'autre ou n'existe pas :
# ne jamais révéler l'existence d'un jeu de données d'un autre propriétaire.
_NOT_FOUND_MESSAGE = "Jeu de données introuvable."

_MAX_LIST_RESULTS = 50
_DESCRIBE_ROW_CAP = 100_000
_MAX_DISTINCT = 1000
_TYPE_INFERENCE_THRESHOLD = 0.9

_DATE_FORMATS = (
    ("%Y-%m-%d", re.compile(r"^\d{4}-\d{2}-\d{2}$")),
    ("%d/%m/%Y", re.compile(r"^\d{2}/\d{2}/\d{4}$")),
    ("%Y-%m", re.compile(r"^\d{4}-\d{2}$")),
)


def _run_async(coro):
    """Run an async coroutine from sync context (ADK tools are sync)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        result = None
        exception = None

        def _run():
            nonlocal result, exception
            try:
                result = asyncio.run(coro)
            except Exception as e:
                exception = e

        t = threading.Thread(target=_run)
        t.start()
        t.join(timeout=30)
        if t.is_alive():
            raise TimeoutError(
                "_run_async: coroutine did not complete within 30 seconds"
            )
        if exception:
            raise exception
        return result
    else:
        return asyncio.run(coro)


@contextlib.asynccontextmanager
async def _get_session():
    """Create a standalone async DB session for use inside tools (see
    business_intelligence._get_session for the full rationale)."""
    from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
    from sqlalchemy.orm import sessionmaker as sa_sessionmaker

    from apowerb.helpers.database_connection import DBConfig

    db_url = DBConfig().get_db_url()

    engine = create_async_engine(
        db_url,
        connect_args={"server_settings": {"jit": "off"}},
        pool_pre_ping=True,
    )
    SessionLocal = sa_sessionmaker(
        autocommit=False, bind=engine, class_=AsyncSession
    )
    session = SessionLocal()
    try:
        yield session
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
        await engine.dispose()


def _agent_owner() -> str:
    """Return the current agent's owner email from environment."""
    import os
    return os.getenv("AGENT_OWNER", "")


# ---------------------------------------------------------------------------
# Date parsing / type inference
# ---------------------------------------------------------------------------


def _parse_date(value: str) -> date | None:
    v = value.strip()
    for fmt, pattern in _DATE_FORMATS:
        if pattern.match(v):
            try:
                parsed = datetime.strptime(v, fmt).date()
            except ValueError:
                return None
            if fmt == "%Y-%m":
                parsed = parsed.replace(day=1)
            return parsed
    return None


def _infer_column_type(non_empty_values: list[str]) -> str:
    if not non_empty_values:
        return "text"
    date_hits = sum(1 for v in non_empty_values if _parse_date(v) is not None)
    if date_hits / len(non_empty_values) >= _TYPE_INFERENCE_THRESHOLD:
        return "date"
    number_hits = 0
    for v in non_empty_values:
        try:
            float(v)
            number_hits += 1
        except ValueError:
            continue
    if number_hits / len(non_empty_values) >= _TYPE_INFERENCE_THRESHOLD:
        return "number"
    return "text"


def _suggested_frequency(distinct_dates: list[date]) -> str | None:
    if len(distinct_dates) < 2:
        return None
    ordered = sorted(distinct_dates)
    gaps = [(b - a).days for a, b in zip(ordered, ordered[1:]) if (b - a).days > 0]
    if not gaps:
        return None
    gaps.sort()
    mid = len(gaps) // 2
    median = gaps[mid] if len(gaps) % 2 else (gaps[mid - 1] + gaps[mid]) / 2
    if median <= 3:
        return "day"
    if median <= 10:
        return "week"
    if median <= 45:
        return "month"
    if median <= 120:
        return "quarter"
    return "year"


def _describe_columns(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows:
        return []
    columns = list(rows[0].keys())
    described = []
    for col in columns:
        raw_values = [r.get(col) for r in rows]
        non_empty = [str(v) for v in raw_values if v is not None and str(v).strip() != ""]
        col_type = _infer_column_type(non_empty)
        distinct = len({str(v) for v in non_empty})
        entry: dict[str, Any] = {
            "name": col,
            "type": col_type,
            "non_null_count": len(non_empty),
            "distinct_count": min(distinct, _MAX_DISTINCT),
        }
        if col_type == "number":
            numbers = [float(v) for v in non_empty]
            if numbers:
                entry["min"] = min(numbers)
                entry["max"] = max(numbers)
        elif col_type == "date":
            parsed = [_parse_date(v) for v in non_empty]
            parsed = [p for p in parsed if p is not None]
            if parsed:
                entry["min"] = min(parsed).isoformat()
                entry["max"] = max(parsed).isoformat()
                entry["suggested_frequency"] = _suggested_frequency(list({p for p in parsed}))
        described.append(entry)
    return described


# ---------------------------------------------------------------------------
# CSV loading (owner-scoped, capped, never silently truncated)
# ---------------------------------------------------------------------------


async def _get_owned_dataset_row(dataset_id: str, owner: str):
    from apowerb.bi.db_stores import DatabaseDataStore

    async with _get_session() as db:
        store = DatabaseDataStore(db, owner=owner)
        return await store.get(dataset_id)


def _parse_csv_rows(text: str, limit: int) -> tuple[list[dict[str, Any]], bool]:
    from apowerb.bi.data.csv_executor import _auto_cast

    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
        delim = dialect.delimiter
    except csv.Error:
        delim = ","

    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    rows: list[dict[str, Any]] = []
    truncated = False
    for i, raw_row in enumerate(reader):
        if i < limit:
            rows.append({k: _auto_cast(v) for k, v in raw_row.items()})
        else:
            truncated = True
            break
    return rows, truncated


async def _load_owned_dataset_rows(dataset_id: str, owner: str, limit: int) -> dict:
    """Lecture limitée au propriétaire, plafonnée à ``limit`` lignes.

    Renvoie ``{"success": False, "error": ...}`` si le jeu n'existe pas ou
    appartient à quelqu'un d'autre (même message dans les deux cas). Sinon
    ``{"success": True, "rows": [...], "truncated": bool, "columns": [...],
    "name": str}`` — ``truncated`` n'est JAMAIS silencieux : c'est à
    l'appelant (l'outil de prévision) de refuser de continuer si True.
    """
    # Un store sans propriétaire ne filtre rien : un owner vide ne lit rien.
    if not owner:
        return {"success": False, "error": _NOT_FOUND_MESSAGE}
    row = await _get_owned_dataset_row(dataset_id, owner)
    if row is None:
        return {"success": False, "error": _NOT_FOUND_MESSAGE}

    config = row.config or {}
    s3_key = config.get("s3_key")
    if not s3_key:
        return {"success": False, "error": _NOT_FOUND_MESSAGE}

    from apowerb.bi.data._bi_storage import read_file

    csv_bytes = read_file(s3_key)
    if csv_bytes is None:
        return {"success": False, "error": _NOT_FOUND_MESSAGE}

    text = csv_bytes.decode("utf-8-sig", errors="replace")
    rows, truncated = _parse_csv_rows(text, limit)
    columns = config.get("columns") or (list(rows[0].keys()) if rows else [])

    return {
        "success": True,
        "rows": rows,
        "truncated": truncated,
        "columns": columns,
        "name": row.name,
    }


def validate_forecast_columns(
    columns: list[str], date_var: str, target_var: str, group_var: str = ""
) -> str | None:
    """Colonne inconnue ⇒ message listant les colonnes disponibles, sinon None."""
    missing = [c for c in (date_var, target_var, group_var or None) if c and c not in columns]
    if not missing:
        return None
    return (
        f"Colonne(s) inconnue(s) : {', '.join(missing)}. "
        f"Colonnes disponibles : {', '.join(columns)}"
    )


# ---------------------------------------------------------------------------
# Agent tools (ADK function-calling interface)
# ---------------------------------------------------------------------------


async def _async_list_datasets(owner: str) -> dict:
    from apowerb.bi.db_stores import DatabaseDataStore

    async with _get_session() as db:
        store = DatabaseDataStore(db, owner=owner)
        rows, _total = await store.list(page=1, page_size=_MAX_LIST_RESULTS)

    datasets = []
    for row in rows:
        config = row.config or {}
        datasets.append({
            "dataset_id": row.id,
            "name": row.name,
            "row_count": config.get("row_count"),
            "columns": config.get("columns") or [],
            "created_at": row.created_at.isoformat() if row.created_at else None,
        })
    return {"success": True, "datasets": datasets}


def tool_list_datasets(folder_name: str = "") -> dict:
    """Lists the CSV datasets imported by this agent's owner.

    Args:
        folder_name: Agent folder name (injected automatically).

    Returns:
        dict with success status and a list of datasets (dataset_id, name,
        row_count, columns, created_at), most recent first, capped at 50.
    """
    owner = _agent_owner()
    if not owner:
        return {"success": False, "error": "No owner context."}
    try:
        return _run_async(_async_list_datasets(owner))
    except Exception as e:
        logger.exception("[BI_DATASETS] tool_list_datasets failed")
        return {"success": False, "error": str(e)}


async def _async_describe_dataset(dataset_id: str, owner: str) -> dict:
    loaded = await _load_owned_dataset_rows(dataset_id, owner, _DESCRIBE_ROW_CAP)
    if not loaded["success"]:
        return loaded

    rows = loaded["rows"]
    return {
        "success": True,
        "dataset_id": dataset_id,
        "name": loaded["name"],
        "columns": _describe_columns(rows),
        "sample_rows": rows[:5],
        # True : statistiques calculées sur les premières lignes seulement.
        "truncated": loaded["truncated"],
    }


def tool_describe_dataset(dataset_id: str, folder_name: str = "") -> dict:
    """Describes a dataset's columns: inferred type, non-null/distinct
    counts, min/max, and — for date columns — a suggested forecast
    frequency, plus 5 sample rows.

    Use this before choosing date_var/target_var/group_var for a forecast:
    it lets the agent pick real columns instead of guessing.

    Args:
        dataset_id:  The dataset_id (UUID) returned by tool_list_datasets.
        folder_name: Agent folder name (injected automatically).

    Returns:
        dict with success status; on success, columns (list of
        {name, type, non_null_count, distinct_count, min, max,
        suggested_frequency}), sample_rows (up to 5) and truncated (True
        when the stats cover only the first 100000 rows). On failure, the
        same "not found" message whether the dataset belongs to someone
        else or does not exist.
    """
    owner = _agent_owner()
    if not owner:
        return {"success": False, "error": "No owner context."}
    try:
        return _run_async(_async_describe_dataset(dataset_id, owner))
    except Exception as e:
        logger.exception("[BI_DATASETS] tool_describe_dataset failed")
        return {"success": False, "error": str(e)}

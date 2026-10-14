"""
bi/spec/sources.py
------------------
Loading of the raw rows behind a :class:`SourceSpec`.

- ``kind=dataset`` → a BI data pool file read through the existing owner-scoped
  helpers (``csv_executor._resolve_s3_key`` + ``_bi_storage.read_file``). The
  owner is the dashboard's owner, never the viewer: a dashboard shared with B
  must stay readable by B without exposing A's other files.
- ``kind=sql`` → ``DatabaseQueryExecutor`` (SELECT only, its own validation),
  scoped to the owner's tool_config.
- ``kind=connector`` → the chart executor of its ``source_type`` (database,
  Google Drive, OneDrive, agent), run as the owner. ``source_type="tool"``
  runs an allowlisted, read-only agent tool as the owner
  (``bi/data/tool_executor.py``, contract in ``tools_store/bi_source.py``).

Both return a DataFrame with normalised column names and inferred dtypes.
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import logging
from typing import Any, AsyncContextManager

import pandas as pd

from apowerb.bi.spec import sql_guard
from apowerb.bi.spec.cache import TTLCache
from apowerb.bi.spec.errors import SpecError
from apowerb.bi.spec.frames import coerce_frame
from apowerb.bi.spec.models import SourceSpec

logger = logging.getLogger(__name__)

MAX_SOURCE_ROWS = 500_000
DATASET_TTL_S = 300.0

_dataset_cache = TTLCache(maxsize=16, ttl=DATASET_TTL_S)


def clear_cache() -> None:
    _dataset_cache.clear()


def parse_dataset_bytes(data: bytes, *, key: str = "", sheet: str | None = None) -> pd.DataFrame:
    """Parse a stored dataset (CSV, or a spreadsheet kept in its native format)."""
    lower = key.lower()
    try:
        if lower.endswith((".xlsx", ".xlsm", ".xls", ".ods")):
            df = pd.read_excel(io.BytesIO(data), sheet_name=sheet or 0)
        else:
            text = data.decode("utf-8-sig", errors="replace")
            try:
                delim = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|").delimiter
            except csv.Error:
                delim = ","
            df = pd.read_csv(io.StringIO(text), sep=delim, nrows=MAX_SOURCE_ROWS + 1)
    except Exception as exc:  # noqa: BLE001 - any parser error is reported the same way
        raise SpecError(f"Lecture du jeu de données impossible : {exc}", en=f"Could not read the dataset: {exc}") from None
    if len(df) > MAX_SOURCE_ROWS:
        raise SpecError(
            f"Jeu de données trop volumineux (plus de {MAX_SOURCE_ROWS} lignes).",
            en=f"Dataset too large (more than {MAX_SOURCE_ROWS} rows).",
        )
    return df


async def load_dataset(dataset_id: str, owner: str, sheet: str | None = None) -> pd.DataFrame:
    """Rows of the owner's dataset ``dataset_id`` (cached a few minutes)."""
    cache_key = (owner.lower(), dataset_id, sheet or "")
    cached = _dataset_cache.get(cache_key)
    if cached is not None:
        return cached

    from apowerb.bi.data._bi_storage import read_file
    from apowerb.bi.data.csv_executor import _resolve_s3_key

    key = await _resolve_s3_key(dataset_id, owner)
    if key is None:
        # Same message whether it does not exist or belongs to someone else.
        raise SpecError(f"Jeu de données introuvable : {dataset_id}", en=f"Dataset not found: {dataset_id}")
    data = await asyncio.to_thread(read_file, key)
    if data is None:
        raise SpecError(f"Jeu de données introuvable : {dataset_id}", en=f"Dataset not found: {dataset_id}")
    df = await asyncio.to_thread(lambda: coerce_frame(parse_dataset_bytes(data, key=key, sheet=sheet)))
    _dataset_cache.set(cache_key, df)
    return df


async def load_sql(connection_config_id: str, query: str, owner: str) -> pd.DataFrame:
    """Rows of a SELECT run on the owner's database connection."""
    from apowerb.bi.charts.core import DataSource
    from apowerb.bi.data.db_executor import DatabaseQueryExecutor

    cleaned = sql_guard.check_query(query, "Requête de la source")
    executor = DatabaseQueryExecutor(connection_config_id, owner_id=owner, max_rows=MAX_SOURCE_ROWS)
    source = DataSource(query=cleaned, connection_config_id=connection_config_id, limit=100_000)
    try:
        rows = await executor.run(source)
    except ValueError as exc:
        raise SpecError(str(exc)) from None
    except RuntimeError as exc:
        logger.warning("[BiSpec] SQL source failed: %s", exc)
        raise SpecError(
            "La requête de la source a échoué (voir les journaux du serveur).",
            en="The source query failed (see the server logs).",
        ) from None
    return coerce_frame(pd.DataFrame.from_records(rows))


def _connector_session() -> AsyncContextManager[Any]:
    """DB session for executors that need one (OneDrive); same factory as datasets."""
    from apowerb.bi.data.csv_executor import _open_session

    return _open_session()


async def load_tool(spec: SourceSpec, owner: str) -> pd.DataFrame:
    """Rows returned by an allowlisted agent tool, run as the dashboard owner."""
    from apowerb.bi.data.tool_executor import ToolQueryExecutor, ToolSourceError

    label = f"Source « {spec.name} »"
    label_en = f"Source “{spec.name}”"
    opts = spec.source_options
    tool, args = opts.get("tool"), opts.get("args") or {}
    if not isinstance(tool, str) or not tool.strip():
        raise SpecError(f"{label} : outil manquant (source_options.tool).", en=f"{label_en}: missing tool (source_options.tool).")
    if not isinstance(args, dict):
        raise SpecError(f"{label} : source_options.args doit être un objet.", en=f"{label_en}: source_options.args must be an object.")
    executor = ToolQueryExecutor(owner, spec.connection_config_id, max_rows=MAX_SOURCE_ROWS)
    try:
        rows = await executor.run(tool.strip(), args)
    except ToolSourceError as exc:
        raise SpecError(f"{label} : {exc}", en=f"{label_en}: {exc}") from None
    except Exception as exc:  # noqa: BLE001 - tool errors are logged, not shown
        logger.warning("[BiSpec] tool source %s failed: %s", spec.name, type(exc).__name__)
        raise SpecError(
            f"{label} : la lecture a échoué (voir les journaux du serveur).",
            en=f"{label_en}: reading failed (see the server logs).",
        ) from None
    return coerce_frame(pd.DataFrame.from_records(rows))


async def load_connector(spec: SourceSpec, owner: str) -> pd.DataFrame:
    """Rows of a ``connector`` source, read like a chart source but as the owner."""
    if spec.source_type == "tool":
        return await load_tool(spec, owner)

    from apowerb.bi.charts.core import DataSource, SourceType
    from apowerb.bi.data import service as data_service

    label = f"Source « {spec.name} »"
    label_en = f"Source “{spec.name}”"
    try:
        source_type = SourceType(spec.source_type or "")
    except ValueError:
        raise SpecError(
            f"{label} : source_type inconnu « {spec.source_type} ».", en=f"{label_en}: unknown source_type “{spec.source_type}”."
        ) from None
    query = spec.query or ""
    if source_type == SourceType.DATABASE:
        query = sql_guard.check_query(query, f"Requête de la {label.lower()}")
    source = DataSource(
        source_type=source_type,
        query=query,
        connection_config_id=spec.connection_config_id,
        source_options=spec.source_options,
        limit=100_000,
    )
    try:
        async with contextlib.AsyncExitStack() as stack:
            db = await stack.enter_async_context(_connector_session()) if source_type == SourceType.ONEDRIVE_EXCEL else None
            executor = data_service.make_executor(
                source, user_id=owner, db_session=db, max_rows=MAX_SOURCE_ROWS, label=label
            )
            rows = await executor.run(source)
    except SpecError:
        raise
    except (data_service.NoDataSourceError, ValueError) as exc:
        raise SpecError(f"{label} : {exc}", en=f"{label_en}: {exc}") from None
    except Exception as exc:  # noqa: BLE001 - provider errors are logged, not shown
        logger.warning("[BiSpec] connector source %s failed: %s", spec.name, exc)
        raise SpecError(
            f"{label} : la lecture a échoué (voir les journaux du serveur).",
            en=f"{label_en}: reading failed (see the server logs).",
        ) from None
    # Some executors report a failure as a single {"error": ...} row.
    if len(rows) == 1 and set(rows[0]) == {"error"}:
        raise SpecError(f"{label} : {rows[0]['error']}", en=f"{label_en}: {rows[0]['error']}")
    return coerce_frame(pd.DataFrame.from_records(rows))


async def load_source(spec: SourceSpec, owner: str) -> pd.DataFrame:
    """Raw frame of a ``dataset``, ``sql`` or ``connector`` source."""
    if spec.kind == "dataset":
        if not spec.dataset_id:
            raise SpecError(f"Source « {spec.name} » : dataset_id manquant.", en=f"Source “{spec.name}”: missing dataset_id.")
        return await load_dataset(spec.dataset_id, owner, spec.sheet)
    if spec.kind == "sql":
        if not spec.connection_config_id or not spec.query:
            raise SpecError(
                f"Source « {spec.name} » : connection_config_id et query sont requis.",
                en=f"Source “{spec.name}”: connection_config_id and query are required.",
            )
        return await load_sql(spec.connection_config_id, spec.query, owner)
    if spec.kind == "connector":
        return await load_connector(spec, owner)
    raise SpecError(f"Source « {spec.name} » : type inattendu {spec.kind}.", en=f"Source “{spec.name}”: unexpected type {spec.kind}.")

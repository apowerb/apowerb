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

Both return a DataFrame with normalised column names and inferred dtypes.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging

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
        raise SpecError(f"Lecture du jeu de données impossible : {exc}") from None
    if len(df) > MAX_SOURCE_ROWS:
        raise SpecError(f"Jeu de données trop volumineux (plus de {MAX_SOURCE_ROWS} lignes).")
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
        raise SpecError(f"Jeu de données introuvable : {dataset_id}")
    data = await asyncio.to_thread(read_file, key)
    if data is None:
        raise SpecError(f"Jeu de données introuvable : {dataset_id}")
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
        raise SpecError("La requête de la source a échoué (voir les journaux du serveur).") from None
    return coerce_frame(pd.DataFrame.from_records(rows))


async def load_source(spec: SourceSpec, owner: str) -> pd.DataFrame:
    """Raw frame of a ``dataset`` or ``sql`` source."""
    if spec.kind == "dataset":
        if not spec.dataset_id:
            raise SpecError(f"Source « {spec.name} » : dataset_id manquant.")
        return await load_dataset(spec.dataset_id, owner, spec.sheet)
    if spec.kind == "sql":
        if not spec.connection_config_id or not spec.query:
            raise SpecError(f"Source « {spec.name} » : connection_config_id et query sont requis.")
        return await load_sql(spec.connection_config_id, spec.query, owner)
    raise SpecError(f"Source « {spec.name} » : type inattendu {spec.kind}.")

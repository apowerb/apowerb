"""CSV file query executor.

Reads data from CSV files uploaded via the BI upload endpoint.
The ``DataSource.query`` is expected to be one of:

    csv://bi/data/{organization_id}/{project_id}/data/{file_id}.csv   (full S3 key)
    csv://{file_id}                                                   (bare UUID — resolved via DB)

The part after ``csv://`` is either the exact S3 key or a file_id
that gets looked up in the ``business_intelligence`` table.

Both forms are resolved scoped to the **chart's owner**
(``CsvQueryExecutor(owner=...)``), never the viewer: a dashboard published
by A must stay readable by a reader B, so the executor is constructed with
``chart.created_by``, not the requesting user. A dataset that doesn't exist
and a dataset owned by someone else are indistinguishable to the caller —
same error message either way.
"""

from __future__ import annotations

import csv
import io
import logging
from typing import Any

from apowerb.bi.charts.core import DataSource
from apowerb.bi.data._bi_storage import read_file

logger = logging.getLogger(__name__)

_S3_KEY_PREFIX = "bi/data/"
_NOT_FOUND_ERROR = "CSV file not found"


async def _resolve_s3_key(raw_key: str, owner: str) -> str | None:
    """Resolve *raw_key* (bare file_id or full S3 key) to a real S3 key,
    scoped to *owner*. Returns ``None`` when no ``type="data"`` row owned
    by *owner* matches — whether the id doesn't exist or belongs to someone
    else."""
    is_full_key = raw_key.startswith(_S3_KEY_PREFIX)

    try:
        from apowerb.helpers.database import sessionmanager
        from apowerb.models import BusinessIntelligence
        from sqlalchemy import func, select

        async with sessionmanager.session() as session:
            clauses = [
                BusinessIntelligence.type == "data",
                func.lower(BusinessIntelligence.owner) == owner.lower(),
            ]
            if not is_full_key:
                clauses.append(BusinessIntelligence.id == raw_key)

            rows = (
                await session.execute(select(BusinessIntelligence).where(*clauses))
            ).scalars().all()

            for row in rows:
                if not row.config:
                    continue
                s3_key = row.config.get("s3_key")
                if not s3_key:
                    continue
                if is_full_key:
                    if s3_key == raw_key:
                        return s3_key
                else:
                    logger.info("[CsvExecutor] Resolved file_id %s → %s", raw_key, s3_key)
                    return s3_key
    except Exception as exc:
        logger.warning("[CsvExecutor] DB lookup failed for %s: %s", raw_key, exc)

    return None


class CsvQueryExecutor:
    """Reads rows from a previously-uploaded CSV file, scoped to the chart's owner."""

    def __init__(self, owner: str) -> None:
        self._owner = owner

    async def run(self, source: DataSource) -> list[dict[str, Any]]:
        raw_key = source.query.removeprefix("csv://").strip()
        if not raw_key:
            return []

        key = await _resolve_s3_key(raw_key, self._owner)
        if key is None:
            return [{"error": _NOT_FOUND_ERROR}]

        csv_bytes = read_file(key)
        if csv_bytes is None:
            return [{"error": _NOT_FOUND_ERROR}]

        text = csv_bytes.decode("utf-8-sig", errors="replace")
        rows: list[dict[str, Any]] = []
        limit = source.limit or 10_000

        try:
            dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
            delim = dialect.delimiter
        except csv.Error:
            delim = ","

        reader = csv.DictReader(io.StringIO(text), delimiter=delim)
        for i, row in enumerate(reader):
            if i >= limit:
                break
            rows.append({k: _auto_cast(v) for k, v in row.items()})

        return rows


def _auto_cast(value: Any) -> Any:
    """Try to cast a CSV string value to int or float."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        return value
    try:
        return int(value)
    except (ValueError, TypeError):
        pass
    try:
        return float(value)
    except (ValueError, TypeError):
        pass
    return value

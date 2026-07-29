"""CSV file query executor.

Reads data from CSV files uploaded via the BI upload endpoint.
The ``DataSource.query`` is expected to be one of:

    csv://bi/data/{organization_id}/{project_id}/data/{file_id}.csv   (full S3 key)
    csv://{file_id}                                                   (bare UUID — resolved via DB)

The part after ``csv://`` is either the exact S3 key or a file_id
that gets looked up in the ``business_intelligence`` table.
"""

from __future__ import annotations

import csv
import io
import logging
from typing import Any

from th2agent.bi.charts.core import DataSource
from th2agent.bi.data._bi_storage import read_file

logger = logging.getLogger(__name__)

_S3_KEY_PREFIX = "bi/data/"


async def _resolve_s3_key(raw_key: str) -> str:
    """If *raw_key* is already a full S3 path, return it as-is.
    Otherwise treat it as a bare file_id and look up the real S3 key in DB."""
    if raw_key.startswith(_S3_KEY_PREFIX):
        return raw_key

    # Bare file_id — look up config.s3_key from the business_intelligence table
    try:
        from th2agent.helpers.database import sessionmanager
        from th2agent.models import BusinessIntelligence
        from sqlalchemy import select

        async with sessionmanager.session() as session:
            row = (
                await session.execute(
                    select(BusinessIntelligence).where(
                        BusinessIntelligence.id == raw_key,
                        BusinessIntelligence.type == "data",
                    )
                )
            ).scalars().first()
            if row and row.config:
                s3_key = row.config.get("s3_key")
                if s3_key:
                    logger.info("[CsvExecutor] Resolved file_id %s → %s", raw_key, s3_key)
                    return s3_key
    except Exception as exc:
        logger.warning("[CsvExecutor] DB lookup failed for %s: %s", raw_key, exc)

    # Fallback: return as-is and let read_file report the error
    return raw_key


class CsvQueryExecutor:
    """Reads rows from a previously-uploaded CSV file."""

    async def run(self, source: DataSource) -> list[dict[str, Any]]:
        raw_key = source.query.removeprefix("csv://").strip()
        if not raw_key:
            return []

        key = await _resolve_s3_key(raw_key)
        csv_bytes = read_file(key)
        if csv_bytes is None:
            return [{"error": f"CSV file not found for key {key}"}]

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

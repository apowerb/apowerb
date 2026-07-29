"""
data/service.py
---------------
Full data pipeline:
  1. Resolve chart config  (via ChartService)
  2. Execute query         (QueryExecutor — database, CSV, or Google Drive)
  3. Apply filters         (chart-level + request-level overrides)
  4. Sort
  5. Paginate
  6. Return ChartDataResponse
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

from th2agent.bi.charts.core import (
    Chart,
    DataSource,
    Filter,
    FilterOperator,
    SortOrder,
    SourceType,
)
from th2agent.bi.data.schema import ChartDataResponse, DataRequest, PageMeta
from th2agent.bi.charts.service import ChartService
from th2agent.bi.data.agent_executor import AgentQueryExecutor
from th2agent.bi.data.db_executor import DatabaseQueryExecutor
from th2agent.bi.data.csv_executor import CsvQueryExecutor
from th2agent.bi.data.google_drive_executor import GoogleDriveQueryExecutor
from th2agent.bi.data.onedrive_excel_executor import OnedriveExcelQueryExecutor

# ---------------------------------------------------------------------------
# QueryExecutor protocol
# ---------------------------------------------------------------------------


class QueryExecutor(Protocol):
    """Pluggable async query runner."""

    async def run(self, source: DataSource) -> list[dict[str, Any]]: ...


class NoDataSourceError(Exception):
    """Raised when a chart has no valid data source configured."""


class QueryExecutionError(Exception):
    """Raised when a chart's data source executor fails at runtime.

    Carries chart_id + source_type so the router can return an actionable
    502 instead of a bare 500, and so failures are never silently cached.
    """

    def __init__(self, chart_id, source_type, cause):
        self.chart_id = chart_id
        self.source_type = source_type
        self.cause = cause
        super().__init__(
            f"Chart '{chart_id}' data fetch failed "
            f"(source={source_type}): {cause}"
        )


# ---------------------------------------------------------------------------
# Filter engine
# ---------------------------------------------------------------------------


class FilterEngine:
    """Applies Filter objects to raw rows in-process."""

    @staticmethod
    def _coerce(row_val: Any, filter_val: Any) -> Any:
        """Cast filter_val to the same type as row_val for safe comparisons."""
        if row_val is None or filter_val is None:
            return filter_val
        try:
            return type(row_val)(filter_val)
        except (ValueError, TypeError):
            return filter_val

    def apply(
        self,
        rows: list[dict[str, Any]],
        filters: list[Filter],
    ) -> list[dict[str, Any]]:
        for f in filters:
            rows = [r for r in rows if self._match(r, f)]
        return rows

    def _match(self, row: dict[str, Any], f: Filter) -> bool:
        row_val = row.get(f.field)
        val = self._coerce(row_val, f.value)
        op = f.operator
        if op == FilterOperator.EQ:
            return row_val == val
        if op == FilterOperator.NEQ:
            return row_val != val
        if op == FilterOperator.GT:
            return (row_val or 0) > val
        if op == FilterOperator.GTE:
            return (row_val or 0) >= val
        if op == FilterOperator.LT:
            return (row_val or 0) < val
        if op == FilterOperator.LTE:
            return (row_val or 0) <= val
        if op == FilterOperator.IN:
            return row_val in (f.value or [])
        if op == FilterOperator.NOT_IN:
            return row_val not in (f.value or [])
        if op == FilterOperator.CONTAINS:
            return str(f.value) in str(row_val or "")
        if op == FilterOperator.IS_NULL:
            return row_val is None
        if op == FilterOperator.IS_NOT_NULL:
            return row_val is not None
        return True


# ---------------------------------------------------------------------------
# Sort engine
# ---------------------------------------------------------------------------


class SortEngine:
    def apply(
        self,
        rows: list[dict[str, Any]],
        sort_field: str | None,
        sort_order: SortOrder,
    ) -> list[dict[str, Any]]:
        if not sort_field:
            return rows
        reverse = sort_order == SortOrder.DESC
        return sorted(
            rows,
            key=lambda r: (r.get(sort_field) is None, r.get(sort_field)),
            reverse=reverse,
        )


# ---------------------------------------------------------------------------
# Paginator
# ---------------------------------------------------------------------------


class Paginator:
    def apply(
        self,
        rows: list[dict[str, Any]],
        page: int,
        page_size: int,
    ) -> tuple[list[dict[str, Any]], PageMeta]:
        total = len(rows)
        start = (page - 1) * page_size
        end = start + page_size
        return rows[start:end], PageMeta(
            page=page,
            page_size=page_size,
            total=total,
            has_next=end < total,
            has_prev=page > 1,
        )


# ---------------------------------------------------------------------------
# In-memory TTL cache for raw query results
# ---------------------------------------------------------------------------

_DEFAULT_CACHE_TTL = 60  # seconds
_MAX_CACHE_ENTRIES = 256


class _QueryCache:
    """Simple in-memory TTL cache for chart query results.

    Keyed on (chart_id, source_query_hash).  Entries expire after
    ``ttl`` seconds.  A global max-size cap evicts oldest entries first.
    """

    def __init__(self, max_size: int = _MAX_CACHE_ENTRIES) -> None:
        self._store: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._max_size = max_size

    @staticmethod
    def _make_key(chart_id: str, source: DataSource) -> str:
        h = hashlib.md5(
            f"{source.source_type}:{source.query}:{source.connection_config_id}".encode(),
            usedforsecurity=False,
        ).hexdigest()
        return f"{chart_id}:{h}"

    def get(self, chart_id: str, source: DataSource, ttl: int) -> list[dict[str, Any]] | None:
        key = self._make_key(chart_id, source)
        entry = self._store.get(key)
        if entry is None:
            return None
        stored_at, rows = entry
        if time.monotonic() - stored_at > ttl:
            del self._store[key]
            return None
        return rows

    def put(self, chart_id: str, source: DataSource, rows: list[dict[str, Any]]) -> None:
        key = self._make_key(chart_id, source)
        # Evict oldest entries if at capacity
        if len(self._store) >= self._max_size and key not in self._store:
            oldest_key = min(self._store, key=lambda k: self._store[k][0])
            del self._store[oldest_key]
        self._store[key] = (time.monotonic(), rows)

    def invalidate(self, chart_id: str) -> None:
        """Remove all cached entries for a given chart."""
        prefix = f"{chart_id}:"
        keys = [k for k in self._store if k.startswith(prefix)]
        for k in keys:
            del self._store[k]


# Singleton cache shared across all ChartDataService instances
_query_cache = _QueryCache()


# ---------------------------------------------------------------------------
# ChartDataService — orchestrates the pipeline
# ---------------------------------------------------------------------------


class ChartDataService:
    """
    Runs the full pipeline for a single data request.

    Usage (FastAPI DI)
    ------------------
    svc = ChartDataService(chart_service)

    @router.get("/charts/{chart_id}/data")
    async def get_data(chart_id: str, req: DataRequest = Depends()):
        return await svc.fetch(chart_id, req)
    """

    def __init__(
        self,
        chart_service: ChartService,
    ) -> None:
        self._charts = chart_service
        self._filters = FilterEngine()
        self._sorter = SortEngine()
        self._pager = Paginator()

    @staticmethod
    def invalidate_cache(chart_id: str) -> None:
        """Remove cached data for a chart (call after update or delete)."""
        _query_cache.invalidate(chart_id)

    async def fetch(
        self,
        chart_id: str,
        req: DataRequest,
        user_id: str | None = None,
        db_session: AsyncSession | None = None,
    ) -> ChartDataResponse:
        # 1. Resolve chart config
        chart: Chart = await self._charts.get(chart_id)

        # 2. Execute query — route to the right executor based on source type.
        #    Use cache to avoid hitting the source on every request.
        ttl = chart.refresh_interval if chart.refresh_interval and chart.refresh_interval > 0 else _DEFAULT_CACHE_TTL
        cached = _query_cache.get(chart_id, chart.source, ttl)

        if cached is not None:
            logger.debug("Cache hit for chart %s", chart_id)
            raw_rows = cached
        else:
            logger.debug("Cache miss for chart %s — executing query", chart_id)
            source_type = chart.source.source_type
            if source_type == SourceType.AGENT:
                if not user_id:
                    raise NoDataSourceError(
                        f"Chart '{chart.id}' uses agent source but no user context provided."
                    )
                executor: QueryExecutor = AgentQueryExecutor(user_id)
            elif source_type == SourceType.GOOGLE_DRIVE:
                if not user_id:
                    raise NoDataSourceError(
                        f"Chart '{chart.id}' uses Google Drive source but no user context provided."
                    )
                executor = GoogleDriveQueryExecutor(
                    chart.source.connection_config_id, owner_id=user_id
                )
            elif source_type == SourceType.ONEDRIVE_EXCEL:
                if not user_id:
                    raise NoDataSourceError(
                        f"Chart '{chart.id}' uses OneDrive Excel source but no user context provided."
                    )
                executor = OnedriveExcelQueryExecutor(
                    owner_id=user_id,
                    db_session=db_session,
                )
            elif source_type == SourceType.CSV or (chart.source.query and chart.source.query.startswith("csv://")):
                executor = CsvQueryExecutor()
            elif chart.source.connection_config_id:
                if not user_id:
                    raise NoDataSourceError(
                        f"Chart '{chart.id}' uses a database source but no user context provided."
                    )
                executor = DatabaseQueryExecutor(
                    chart.source.connection_config_id, owner_id=user_id
                )
            else:
                raise NoDataSourceError(
                    f"Chart '{chart.id}' has no valid data source configured. "
                    f"A database connection, CSV file, Google Drive, OneDrive Excel, or agent source is required."
                )
            try:
                raw_rows = await executor.run(chart.source)
            except NoDataSourceError:
                raise
            except Exception as exc:
                logger.exception(
                    "Chart %s data fetch failed (source_type=%s)",
                    chart_id, source_type,
                )
                raise QueryExecutionError(chart_id, source_type, exc) from exc
            # Only successful results are cached — never poison the cache
            # with a transient failure.
            _query_cache.put(chart_id, chart.source, raw_rows)

        # 3. Apply filters: chart-level first, then request-level overrides
        all_filters = list(chart.filters) + req.extra_filters()
        rows = self._filters.apply(raw_rows, all_filters)

        # 4. Sort — prefer request override, fall back to chart source config
        sort_field = req.sort_field or (
            chart.source.sort.field if chart.source.sort else None
        )
        sort_order = (
            req.sort_order
            if req.sort_field
            else (chart.source.sort.order if chart.source.sort else SortOrder.DESC)
        )
        rows = self._sorter.apply(rows, sort_field, sort_order)

        # 5. Paginate
        # Resolve effective page size: explicit request > chart.source.limit > 50.
        # This honours the per-chart operator setting (cf DataSource.limit,
        # default 1000) instead of silently truncating tables to 50 rows.
        effective_page_size = (
            req.page_size
            if req.page_size is not None
            else (chart.source.limit or 50)
        )
        page_rows, pagination = self._pager.apply(rows, req.page, effective_page_size)

        # 6. Derive labels from first row keys
        labels = list(page_rows[0].keys()) if page_rows else []

        return ChartDataResponse(
            chart_id=chart.id,
            chart_type=chart.chart_type,
            title=chart.title,
            description=chart.description,
            labels=labels,
            rows=page_rows,
            pagination=pagination,
            refresh_interval=chart.refresh_interval,
            config=chart.config if chart.config else None,
        )

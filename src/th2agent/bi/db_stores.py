"""DB-backed stores for Charts and Dashboards.

Persists chart/dashboard configs and metadata in the ``business_intelligence`` table.
Heavier data (CSVs) is stored in S3 via separate mechanisms (see ``data._bi_storage.py``).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import TypeVar

logger = logging.getLogger(__name__)

from sqlalchemy import select, func, ColumnElement
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from th2agent.bi.charts.core import Chart
from th2agent.bi.dashboards.core import Dashboard
from th2agent.bi.data._bi_storage import delete_file as delete_s3_file
from th2agent.models import BIItemStatus, BusinessIntelligence

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Base store — shared owner-scoped query logic
# ---------------------------------------------------------------------------


class _BaseBIStore:
    """Common DB helpers for BI stores backed by the ``business_intelligence`` table."""

    _bi_type: str  # "chart" or "dashboard", set by subclasses

    def __init__(self, db: AsyncSession, owner: str | None = None) -> None:
        self._db = db
        self._owner = owner

    def _base_filters(self) -> list[ColumnElement[bool]]:
        """Standard filters: type + not deleted + owner scope."""
        clauses = [
            BusinessIntelligence.type == self._bi_type,
            BusinessIntelligence.status != BIItemStatus.DELETED,
        ]
        if self._owner:
            clauses.append(func.lower(BusinessIntelligence.owner) == self._owner.lower())
        return clauses

    async def _get_row(self, item_id: str) -> BusinessIntelligence | None:
        """Fetch a row by PK, enforcing type and owner scope."""
        row = await self._db.get(BusinessIntelligence, item_id)
        if row is None or row.type != self._bi_type:
            return None
        if row.status == BIItemStatus.DELETED:
            return None
        if self._owner and row.owner.lower() != self._owner.lower():
            return None
        return row

    async def _soft_delete(self, item_id: str) -> bool:
        """Mark a row as deleted (soft delete) instead of removing it."""
        row = await self._get_row(item_id)
        if not row:
            return False
        row.status = BIItemStatus.DELETED
        row.updated_at = datetime.now(timezone.utc)
        await self._db.commit()
        logger.info("Soft-deleted %s %s", self._bi_type, item_id)
        return True

    async def count(self) -> int:
        q = select(func.count()).select_from(BusinessIntelligence).where(
            *self._base_filters(),
        )
        return (await self._db.execute(q)).scalar() or 0


# ---------------------------------------------------------------------------
# Chart DB Store
# ---------------------------------------------------------------------------


class DatabaseChartStore(_BaseBIStore):
    """Persists Chart objects in the business_intelligence table."""

    _bi_type = "chart"

    async def get(self, chart_id: str) -> Chart | None:
        row = await self._get_row(chart_id)
        if not row:
            return None
        if not row.config:
            logger.warning("Chart row %s has no config", row.id)
            return None
        try:
            return Chart(**row.config)
        except Exception as e:
            logger.warning("Chart row %s has invalid config: %s", row.id, e)
            return None

    async def list(self, page: int = 1, page_size: int = 20) -> tuple[list[Chart], int]:
        filters = self._base_filters()
        count_q = select(func.count()).select_from(BusinessIntelligence).where(*filters)
        total = (await self._db.execute(count_q)).scalar() or 0

        q = (
            select(BusinessIntelligence)
            .where(*filters)
            .order_by(BusinessIntelligence.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        rows = (await self._db.execute(q)).scalars().all()

        charts = []
        for r in rows:
            if not r.config:
                logger.warning("Chart row %s has no config, skipping", r.id)
                continue
            try:
                charts.append(Chart(**r.config))
            except Exception as e:
                logger.warning("Chart row %s has invalid config, skipping: %s", r.id, e)
        return charts, total

    async def save(self, chart: Chart) -> Chart:
        row = await self._db.get(BusinessIntelligence, chart.id)

        # Always save full config to DB
        config_data = json.loads(chart.model_dump_json())

        if row:
            # Prevent resurrecting a soft-deleted row
            if row.status == BIItemStatus.DELETED:
                row = None
            elif self._owner and row.owner.lower() != self._owner.lower():
                return chart

        if row:
            row.name = chart.name
            row.config = config_data
            row.organization_id = chart.organization_id
            row.project_id = chart.project_id
            row.permissions = chart.permissions
            row.updated_at = datetime.now(timezone.utc)
        else:
            row = BusinessIntelligence(
                id=chart.id,
                name=chart.name,
                type=self._bi_type,
                owner=(self._owner or chart.created_by or "system").lower(),
                organization_id=chart.organization_id,
                project_id=chart.project_id,
                permissions=chart.permissions,
                config=config_data,
                status="active",
            )
            self._db.add(row)
        await self._db.commit()
        return chart

    async def delete(self, chart_id: str) -> bool:
        return await self._soft_delete(chart_id)


# ---------------------------------------------------------------------------
# Dashboard DB Store
# ---------------------------------------------------------------------------


class DatabaseDashboardStore(_BaseBIStore):
    """Persists Dashboard objects in the business_intelligence table."""

    _bi_type = "dashboard"

    async def get(self, dashboard_id: str) -> Dashboard | None:
        row = await self._get_row(dashboard_id)
        if not row:
            return None
        if not row.config:
            logger.warning("Dashboard row %s has no config", row.id)
            return None
        try:
            return Dashboard(**row.config)
        except Exception as e:
            logger.warning("Dashboard row %s has invalid config: %s", row.id, e)
            return None

    async def get_by_slug(self, slug: str) -> Dashboard | None:
        import re

        filters = self._base_filters()
        # 1. Try exact slug match
        q = select(BusinessIntelligence).where(
            *filters,
            BusinessIntelligence.config["slug"].as_string() == slug,
        )
        row = (await self._db.execute(q)).scalars().first()
        if row and row.config:
            try:
                return Dashboard(**row.config)
            except Exception as e:
                logger.warning("Dashboard row %s (slug match) has invalid config: %s", row.id, e)

        # 2. Fallback: match by slugified title (for dashboards published before slug was added)
        q_all = select(BusinessIntelligence).where(*filters)
        rows = (await self._db.execute(q_all)).scalars().all()
        for r in rows:
            if r.config:
                title = r.config.get("title", "")
                title_slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
                if title_slug == slug:
                    try:
                        return Dashboard(**r.config)
                    except Exception as e:
                        logger.warning("Dashboard row %s (title-slug match) has invalid config: %s", r.id, e)

        # 3. Backwards-compat fallback: if slug looks like a UUID, try matching by id.
        # Old share URLs used the dashboard id instead of the slug — keep them working.
        if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", slug):
            q_id = select(BusinessIntelligence).where(*filters, BusinessIntelligence.id == slug)
            row = (await self._db.execute(q_id)).scalars().first()
            if row and row.config:
                try:
                    return Dashboard(**row.config)
                except Exception as e:
                    logger.warning("Dashboard row %s (uuid fallback) has invalid config: %s", row.id, e)

        return None

    async def list(
        self,
        page: int = 1,
        page_size: int = 20,
        organization_id: str | None = None,
        project_id: str | None = None,
    ) -> tuple[list[Dashboard], int]:
        filters = self._base_filters()

        if organization_id:
            filters.append(BusinessIntelligence.organization_id == organization_id)

        if project_id:
            filters.append(BusinessIntelligence.project_id == project_id)

        count_q = select(func.count()).select_from(BusinessIntelligence).where(
            *filters, BusinessIntelligence.config.isnot(None),
        )
        total = (await self._db.execute(count_q)).scalar() or 0

        q = (
            select(BusinessIntelligence)
            .where(*filters, BusinessIntelligence.config.isnot(None))
            .order_by(BusinessIntelligence.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        rows = (await self._db.execute(q)).scalars().all()
        dashboards = []
        for r in rows:
            if not r.config:
                logger.warning("Dashboard row %s has no config, skipping", r.id)
                continue
            try:
                dashboards.append(Dashboard(**r.config))
            except Exception as e:
                logger.warning("Dashboard row %s has invalid config, skipping: %s", r.id, e)
        return dashboards, total

    async def save(self, dashboard: Dashboard) -> Dashboard:
        from th2agent.helpers.emails import get_domain_from_email

        row = await self._db.get(BusinessIntelligence, dashboard.id)
        config_data = json.loads(dashboard.model_dump_json())
        owner = (self._owner or dashboard.created_by or "system").lower()

        try:
            org_id = get_domain_from_email(owner) if self._owner else "default"
        except (ValueError, IndexError):
            org_id = "default"

        if row:
            # Prevent resurrecting a soft-deleted row
            if row.status == BIItemStatus.DELETED:
                row = None
            elif self._owner and row.owner.lower() != self._owner.lower():
                return dashboard

        if row:
            row.name = dashboard.title
            row.config = config_data
            row.permissions = dashboard.permissions
            row.organization_id = dashboard.organization_id or org_id
            row.project_id = dashboard.project_id or row.project_id
            row.updated_at = datetime.now(timezone.utc)
        else:
            row = BusinessIntelligence(
                id=dashboard.id,
                name=dashboard.title,
                type=self._bi_type,
                owner=owner,
                organization_id=dashboard.organization_id or org_id,
                project_id=dashboard.project_id or "thaink2",
                permissions=dashboard.permissions,
                config=config_data,
                status="active",
            )
            self._db.add(row)
        await self._db.commit()
        return dashboard

    async def delete(self, dashboard_id: str) -> bool:
        return await self._soft_delete(dashboard_id)


# ---------------------------------------------------------------------------
# Data file DB Store
# ---------------------------------------------------------------------------


class DatabaseDataStore(_BaseBIStore):
    """Persists BI data-file metadata in the business_intelligence table.

    The actual file bytes (CSV, XLSX, JSON, etc.) stay in S3.
    Only lightweight metadata + the S3 key are stored in ``config``.
    """

    _bi_type = "data"

    async def get(self, file_id: str) -> BusinessIntelligence | None:
        return await self._get_row(file_id)

    async def list(
        self,
        page: int = 1,
        page_size: int = 20,
        organization_id: str | None = None,
        project_id: str | None = None,
    ) -> tuple[list[BusinessIntelligence], int]:
        filters = self._base_filters()

        if organization_id:
            filters.append(BusinessIntelligence.organization_id == organization_id)

        if project_id:
            filters.append(BusinessIntelligence.project_id == project_id)

        count_q = select(func.count()).select_from(BusinessIntelligence).where(*filters)
        total = (await self._db.execute(count_q)).scalar() or 0

        q = (
            select(BusinessIntelligence)
            .where(*filters)
            .order_by(BusinessIntelligence.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        rows = (await self._db.execute(q)).scalars().all()
        return rows, total

    async def save(
        self,
        *,
        file_id: str,
        name: str,
        organization_id: str,
        project_id: str,
        permissions: list[str] | None = None,
        metadata: dict | None = None,
    ) -> BusinessIntelligence:
        row = await self._db.get(BusinessIntelligence, file_id)

        config_data = metadata or {}
        permissions_data = permissions or []

        if row:
            # Prevent resurrecting a soft-deleted row
            if row.status == BIItemStatus.DELETED:
                row = None
            elif self._owner and row.owner.lower() != self._owner.lower():
                return row

        if row:
            row.name = name
            row.organization_id = organization_id
            row.project_id = project_id
            row.permissions = permissions_data
            row.config = config_data
            row.updated_at = datetime.now(timezone.utc)
        else:
            row = BusinessIntelligence(
                id=file_id,
                name=name,
                type=self._bi_type,
                owner=self._owner or "system",
                organization_id=organization_id,
                project_id=project_id,
                permissions=permissions_data,
                config=config_data,
                status="active",
            )
            self._db.add(row)
        try:
            await self._db.commit()
        except IntegrityError:
            await self._db.rollback()
            raise

        return row

    async def delete(self, file_id: str) -> bool:
        row = await self._get_row(file_id)
        if not row:
            return False
        # Clean up the S3 file before soft-deleting the DB record
        s3_key = (row.config or {}).get("s3_key")
        if s3_key:
            if not delete_s3_file(s3_key):
                logger.warning("S3 cleanup failed for data %s (key=%s), proceeding with soft delete", file_id, s3_key)
        return await self._soft_delete(file_id)

    async def get_scoped(
        self,
        file_id: str,
        organization_id: str,
        project_id: str,
    ) -> BusinessIntelligence | None:
        filters = self._base_filters() + [
            BusinessIntelligence.id == file_id,
            BusinessIntelligence.organization_id == organization_id,
            BusinessIntelligence.project_id == project_id,
        ]
        q = select(BusinessIntelligence).where(*filters)
        return (await self._db.execute(q)).scalars().first()


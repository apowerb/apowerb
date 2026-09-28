"""
dashboards/access.py
--------------------
Who may see a published dashboard, and the charts it displays.

One rule for /dashboards/public/{slug}, /dashboards/shared and
/public/charts/{chart_id}/data. Callers check authentication first.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apowerb.bi.dashboards.core import Dashboard, DashboardStatus, DashboardVisibility
from apowerb.helpers.emails import get_domain_from_email_or_none
from apowerb.models import BIItemStatus, BusinessIntelligence

logger = logging.getLogger(__name__)


def visible_to(dashboard: Dashboard, viewer_email: str) -> bool:
    """Visibility rule of a PUBLISHED dashboard for a logged-in viewer."""
    # Published dashboards default to public visibility (backwards compat)
    if dashboard.visibility in (DashboardVisibility.PUBLIC, DashboardVisibility.PRIVATE):
        return True
    if dashboard.visibility == DashboardVisibility.ORGANIZATION:
        owner_domain = get_domain_from_email_or_none(dashboard.created_by)
        viewer_domain = get_domain_from_email_or_none(viewer_email)
        return owner_domain is not None and viewer_domain is not None \
            and viewer_domain.lower() == owner_domain.lower()
    return False


def _shows_chart(config: dict, chart_id: str) -> bool:
    return any(
        ((component or {}).get("chart") or {}).get("chart_id") == chart_id
        for component in config.get("components") or []
    )


async def chart_on_visible_dashboard(db: AsyncSession, chart_id: str, viewer_email: str) -> bool:
    """True when a PUBLISHED dashboard visible to the viewer displays the chart."""
    rows = (
        await db.execute(
            select(BusinessIntelligence.owner, BusinessIntelligence.config).where(
                BusinessIntelligence.type == "dashboard",
                BusinessIntelligence.status != BIItemStatus.DELETED,
                BusinessIntelligence.config["status"].as_string() == DashboardStatus.PUBLISHED.value,
            )
        )
    ).all()
    for owner, config in rows:
        if not config or not _shows_chart(config, chart_id):
            continue
        try:
            # The row's owner column decides the organization, not the config copy.
            dashboard = Dashboard(**{**config, "created_by": owner})
        except Exception as exc:
            logger.warning("[DashboardAccess] invalid dashboard config skipped: %s", exc)
            continue
        if dashboard.status == DashboardStatus.PUBLISHED and visible_to(dashboard, viewer_email):
            return True
    return False

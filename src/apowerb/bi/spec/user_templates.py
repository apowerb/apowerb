"""
bi/spec/user_templates.py
-------------------------
Templates made by users from one of their dashboards.

A user template is a ``business_intelligence`` row of type ``dashboard_template``
holding a frozen copy of a dashboard's sources, filters and visuals, with the
dataset replaced by a marker and one column slot per real column of the dataset
source. Building a dashboard from it substitutes the dataset and renames the
user's columns to the frozen names (see ``lifecycle.apply_mapping``).

Visibility is the one of published dashboards (``access.shared_with``): a private
template is only seen by its owner, an organization one by every user of the
owner's email domain.
"""

from __future__ import annotations

import copy
import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from apowerb.bi.dashboards.access import shared_with
from apowerb.bi.dashboards.core import Dashboard, DashboardVisibility
from apowerb.bi.dashboards.service import DashboardNameConflictError
from apowerb.bi.spec.catalog import DashboardTemplate, TemplateColumn
from apowerb.bi.spec.errors import SpecError
from apowerb.bi.spec.lifecycle import apply_mapping, pure_renames, root_dataset_source, source_columns
from apowerb.bi.spec.models import SourceSpec
from apowerb.helpers.emails import get_domain_from_email_or_none
from apowerb.models import BIItemStatus, BusinessIntelligence

logger = logging.getLogger(__name__)

USER_TEMPLATE_TYPE = "dashboard_template"
DATASET_MARKER = "{{dataset}}"
USER_TEMPLATE_VERSION = 1
_PROJECT_ID = "thaink2"


def freeze(dashboard: Dashboard, label: str, description: str, visibility: str) -> dict[str, Any]:
    """Config of a user template from ``dashboard`` (raises SpecError when it cannot be one)."""
    if dashboard.spec_version < 2:
        raise SpecError("Seul un tableau de bord à la spécification v2 peut devenir un modèle.")
    root = root_dataset_source(dashboard.sources)
    _, transforms = pure_renames(root.transforms)
    frozen_root = root.model_copy(update={"dataset_id": DATASET_MARKER, "transforms": transforms})
    sources = [frozen_root if s.name == root.name else s for s in dashboard.sources]
    columns = [
        {"key": c.key, "label": c.label, "type": c.type, "required": c.required, "default": c.key}
        for c in source_columns(dashboard)
    ]
    return {
        "label": label,
        "description": description,
        "visibility": visibility,
        "version": USER_TEMPLATE_VERSION,
        "columns": columns,
        "spec": {
            "sources": [s.model_dump(mode="json") for s in sources],
            "filters": [f.model_dump(mode="json") for f in dashboard.filters],
            "components": [
                {"position": c.position.model_dump(mode="json"), "visual": c.visual.model_dump(mode="json")}
                for c in dashboard.components
                if c.visual is not None
            ],
        },
    }


def _builder(config: dict[str, Any]):
    def build(dataset_id: str, columns: dict[str, str | None], params: dict[str, Any]) -> dict:
        spec = copy.deepcopy(config["spec"])
        for index, raw in enumerate(spec["sources"]):
            if raw.get("kind") == "dataset":
                source = SourceSpec.model_validate({**raw, "dataset_id": dataset_id})
                source = source.model_copy(update={"transforms": apply_mapping(source.transforms, columns)})
                spec["sources"][index] = source.model_dump(mode="json")
        return spec

    return build


def to_template(row: BusinessIntelligence, viewer_email: str) -> DashboardTemplate:
    config = row.config or {}
    return DashboardTemplate(
        id=row.id,
        label=config.get("label") or row.name,
        description=config.get("description") or "",
        columns=tuple(TemplateColumn(**c) for c in config.get("columns") or []),
        params=(),
        build=_builder(config),
        version=int(config.get("version") or USER_TEMPLATE_VERSION),
        origin="user",
        owner=row.owner,
        visibility=config.get("visibility"),
        can_delete=row.owner.lower() == (viewer_email or "").lower(),
    )


class UserTemplateStore:
    def __init__(self, db: AsyncSession, viewer_email: str) -> None:
        self._db = db
        self._viewer = viewer_email

    def _visible(self, row: BusinessIntelligence) -> bool:
        try:
            visibility = DashboardVisibility((row.config or {}).get("visibility"))
        except ValueError:
            return False
        return shared_with(visibility, row.owner, self._viewer)

    async def _row(self, template_id: str) -> BusinessIntelligence | None:
        row = await self._db.get(BusinessIntelligence, template_id)
        if row is None or row.type != USER_TEMPLATE_TYPE or row.status == BIItemStatus.DELETED:
            return None
        return row if self._visible(row) else None

    async def get(self, template_id: str) -> DashboardTemplate | None:
        row = await self._row(template_id)
        return to_template(row, self._viewer) if row else None

    async def list_visible(self) -> list[DashboardTemplate]:
        domain = get_domain_from_email_or_none(self._viewer)
        scope = [func.lower(BusinessIntelligence.owner) == (self._viewer or "").lower()]
        if domain:
            scope.append(BusinessIntelligence.organization_id == domain)
        query = (
            select(BusinessIntelligence)
            .where(
                BusinessIntelligence.type == USER_TEMPLATE_TYPE,
                BusinessIntelligence.status != BIItemStatus.DELETED,
                or_(*scope),
            )
            .order_by(BusinessIntelligence.created_at, BusinessIntelligence.name)
        )
        rows = (await self._db.execute(query)).scalars().all()
        return [to_template(r, self._viewer) for r in rows if self._visible(r)]

    async def create(self, dashboard: Dashboard, label: str, description: str, visibility: str) -> DashboardTemplate:
        config = json.loads(json.dumps(freeze(dashboard, label, description, visibility)))
        owner = self._viewer.lower()
        row = BusinessIntelligence(
            name=label,
            type=USER_TEMPLATE_TYPE,
            owner=owner,
            organization_id=get_domain_from_email_or_none(owner) or "default",
            project_id=_PROJECT_ID,
            permissions=[],
            config=config,
            status=BIItemStatus.ACTIVE.value,
            created_at=datetime.now(timezone.utc),
        )
        self._db.add(row)
        try:
            await self._db.commit()
        except IntegrityError as exc:
            await self._db.rollback()
            raise DashboardNameConflictError(label) from exc
        # The commit expires the row (expire_on_commit): reload it before reading its columns.
        await self._db.refresh(row)
        return to_template(row, self._viewer)

    async def delete(self, template_id: str) -> None:
        row = await self._db.get(BusinessIntelligence, template_id)
        if row is not None:
            await self._db.delete(row)
            await self._db.commit()

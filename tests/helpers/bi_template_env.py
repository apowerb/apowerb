"""Helpers for the template lifecycle tests: dashboards from a spec dict and
synthetic frames (generic families, seeded)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import MagicMock, patch

import httpx
import numpy as np
import pandas as pd
from fastapi import FastAPI
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from apowerb.bi.dashboards.core import ComponentType, Dashboard, DashboardComponent, GridPosition
from apowerb.bi.spec import engine as spec_engine
from apowerb.bi.spec import sources
from apowerb.bi.spec.errors import SpecError
from apowerb.bi.spec.models import FilterSpec, SourceSpec, VisualSpec
from apowerb.models import BusinessIntelligence
from tests.helpers.bi_spec_data import weekly_sales

ALICE = "alice@example.com"
BOB = "bob@example.com"
CAROL = "carol@other.org"

RENAMED = {"semaine": "week", "famille": "product_group", "ventes": "qty"}


def dashboard_from_spec(spec: dict, created_by: str = "alice@example.com", title: str = "Pilotage") -> Dashboard:
    return Dashboard.create(
        title,
        sources=[SourceSpec.model_validate(s) for s in spec["sources"]],
        filters=[FilterSpec.model_validate(f) for f in spec["filters"]],
        components=[
            DashboardComponent(
                component_type=ComponentType.VISUAL,
                position=GridPosition(**c["position"]),
                visual=VisualSpec.model_validate(c["visual"]),
            )
            for c in spec["components"]
        ],
        spec_version=2,
        created_by=created_by,
    )


def sales_frame(weeks: int = 130, renamed: bool = False) -> pd.DataFrame:
    frame = weekly_sales(weeks=weeks)[["semaine", "famille", "ventes"]]
    return frame.rename(columns=RENAMED) if renamed else frame


def stock_frame(weeks: int = 60, renamed: bool = False) -> pd.DataFrame:
    """Weekly sales plus a real stock, a safety threshold and a supplier lead time."""
    frame = weekly_sales(weeks=weeks)[["semaine", "famille", "ventes"]].copy()
    rng = np.random.default_rng(3)
    frame["stock"] = np.round(frame["ventes"] * rng.uniform(1.5, 3.0, len(frame)))
    frame["seuil_securite"] = np.round(frame["ventes"] * 0.5)
    frame["delai_fournisseur_sem"] = 2
    if renamed:
        frame = frame.rename(columns={**RENAMED, "stock": "on_hand", "seuil_securite": "min_level", "delai_fournisseur_sem": "lead_weeks"})
    return frame


class DbEnv:
    """Spec router on a real SQLite ``business_intelligence`` table and the real engine.
    Only the dataset loader is replaced: ``datasets`` maps a dataset id to (owner, frame)."""

    def __init__(self, maker) -> None:
        self.maker = maker
        self.datasets: dict[str, tuple[str, pd.DataFrame]] = {}

    def client(self, viewer: str = ALICE) -> httpx.AsyncClient:
        from apowerb.auth.dependencies import get_current_user
        from apowerb.bi.spec.router import router as spec_router
        from apowerb.helpers.database import get_db

        app = FastAPI()
        app.include_router(spec_router, prefix="/api/v1")

        async def db():
            async with self.maker() as session:
                yield session

        user = MagicMock()
        user.email = viewer
        app.dependency_overrides[get_db] = db
        app.dependency_overrides[get_current_user] = lambda: user
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    def patched(self):
        datasets = self.datasets

        async def fake_load(dataset_id, owner, sheet=None):
            entry = datasets.get(dataset_id)
            if entry is None or entry[0].lower() != owner.lower():
                raise SpecError(f"Jeu de données introuvable : {dataset_id}")
            return entry[1]

        return patch.object(sources, "load_dataset", fake_load)

    async def save(self, dashboard: Dashboard, owner: str | None = None) -> Dashboard:
        from apowerb.bi.db_stores import DatabaseDashboardStore

        async with self.maker() as session:
            return await DatabaseDashboardStore(session, owner=owner or dashboard.created_by).save(dashboard)

    async def load(self, dashboard_id: str) -> Dashboard | None:
        from apowerb.bi.db_stores import DatabaseDashboardStore

        async with self.maker() as session:
            return await DatabaseDashboardStore(session, owner=None).get(dashboard_id)


@asynccontextmanager
async def db_env() -> AsyncIterator[DbEnv]:
    schema = BusinessIntelligence.__table__.schema
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    if schema:
        @event.listens_for(engine.sync_engine, "connect")
        def _attach(conn, _):
            conn.execute(f"ATTACH DATABASE ':memory:' AS {schema}")

    async with engine.begin() as conn:
        await conn.run_sync(lambda c: BusinessIntelligence.__table__.create(c))
    spec_engine.clear_caches()
    try:
        yield DbEnv(async_sessionmaker(engine, expire_on_commit=False))
    finally:
        spec_engine.clear_caches()
        await engine.dispose()

"""Titres dédoublonnés sur une vraie base : la contrainte d'unicité
(nom, type, organisation, projet) lève une IntegrityError à l'enregistrement,
la session doit rester utilisable pour l'essai suivant."""

from __future__ import annotations

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from apowerb.bi.dashboards.schema import DashboardCreateRequest
from apowerb.bi.dashboards.service import DashboardService, create_with_unique_title
from apowerb.models import BusinessIntelligence

OWNER = "alice@example.com"


@pytest.fixture
async def session():
    schema = BusinessIntelligence.__table__.schema
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    if schema:
        @event.listens_for(engine.sync_engine, "connect")
        def _attach(conn, _):
            conn.execute(f"ATTACH DATABASE ':memory:' AS {schema}")

    async with engine.begin() as conn:
        await conn.run_sync(lambda c: BusinessIntelligence.__table__.create(c))
    async with async_sessionmaker(engine)() as s:
        yield s
    await engine.dispose()


async def test_same_title_three_times_on_one_session(session):
    from apowerb.bi.db_stores import DatabaseDashboardStore

    svc = DashboardService(DatabaseDashboardStore(session, owner=OWNER))

    async def create(title: str):
        return await create_with_unique_title(
            title, lambda candidate: svc.create(DashboardCreateRequest(title=candidate), created_by=OWNER)
        )

    titles = [(await create("Pilotage stocks"))[1] for _ in range(3)]

    assert titles == ["Pilotage stocks", "Pilotage stocks (2)", "Pilotage stocks (3)"]
    rows = (await session.execute(select(BusinessIntelligence.name).order_by(BusinessIntelligence.name))).scalars().all()
    assert rows == titles

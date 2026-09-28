"""Le propriétaire d'un tableau de bord est la colonne ``owner`` de sa ligne.

``Dashboard.created_by`` vit dans la config JSON ; le domaine qui décide de la
visibilité ``organization`` et le filtre « mes tableaux » de /dashboards/shared
doivent venir de la ligne, pas de ce champ recopié.
"""

from __future__ import annotations

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from apowerb.bi.dashboards.core import (
    ChartWidget,
    Dashboard,
    DashboardComponent,
    DashboardStatus,
    DashboardVisibility,
)
from apowerb.models import BusinessIntelligence

OWNER = "alice@example.com"
FORGED = "mallory@other.org"


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


async def _add_dashboard(session, *, visibility=DashboardVisibility.ORGANIZATION, chart_ids=()):
    """Row owned by OWNER whose config claims FORGED as its creator."""
    dashboard = Dashboard.create(title="Ventes", slug="ventes", created_by=FORGED).model_copy(update={
        "status": DashboardStatus.PUBLISHED,
        "visibility": visibility,
        "components": [
            DashboardComponent(component_type="chart", chart=ChartWidget(chart_id=c)) for c in chart_ids
        ],
    })
    session.add(BusinessIntelligence(
        id=dashboard.id, name=dashboard.title, type="dashboard", owner=OWNER,
        organization_id="example.com", config=dashboard.model_dump(mode="json"),
    ))
    await session.commit()
    return dashboard


class TestStoreReturnsTheRowOwner:
    async def test_get(self, session):
        from apowerb.bi.db_stores import DatabaseDashboardStore

        d = await _add_dashboard(session)
        assert (await DatabaseDashboardStore(session).get(d.id)).created_by == OWNER

    async def test_get_by_slug(self, session):
        from apowerb.bi.db_stores import DatabaseDashboardStore

        await _add_dashboard(session)
        assert (await DatabaseDashboardStore(session).get_by_slug("ventes")).created_by == OWNER

    async def test_list(self, session):
        from apowerb.bi.db_stores import DatabaseDashboardStore

        await _add_dashboard(session)
        dashboards, _ = await DatabaseDashboardStore(session).list()
        assert [d.created_by for d in dashboards] == [OWNER]


class TestOrganizationVisibilityUsesTheRowOwner:
    async def test_forged_domain_does_not_admit_its_users(self, session):
        from apowerb.bi.dashboards.access import chart_on_visible_dashboard

        await _add_dashboard(session, chart_ids=["chart1"])
        assert not await chart_on_visible_dashboard(session, "chart1", "eve@other.org")

    async def test_owner_domain_admits_its_users(self, session):
        from apowerb.bi.dashboards.access import chart_on_visible_dashboard

        await _add_dashboard(session, chart_ids=["chart1"])
        assert await chart_on_visible_dashboard(session, "chart1", "bob@example.com")


class TestSharedSkipsOwnDashboardsIgnoringCase:
    def test_owner_with_other_casing_does_not_see_their_own_dashboard(self):
        from tests.test_dashboards_shared import _dash, _patched_client

        client, p = _patched_client(
            [_dash(id_="d1", created_by=OWNER, visibility=DashboardVisibility.PUBLIC)],
            email="Alice@Example.com",
        )
        try:
            resp = client.get("/api/v1/dashboards/shared")
        finally:
            p.stop()
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

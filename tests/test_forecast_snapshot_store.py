"""ForecastSnapshotStore contre une vraie base (SQLite en mémoire) : upsert
sans doublon et rétention à 60 par graphique (contrat étape 5 §3).

Même patron que tests/usage/test_usage_quota_db.py : StaticPool + ATTACH pour
le schéma qualifié, une seule table créée.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from apowerb.bi.forecast_snapshot_store import RETENTION_PER_CHART, ForecastSnapshotStore
from apowerb.helpers.database import Base
from apowerb.models import BIForecastSnapshot


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    schema = BIForecastSnapshot.__table__.schema
    async with engine.begin() as conn:
        if schema:
            await conn.execute(text(f"ATTACH DATABASE ':memory:' AS {schema}"))
        await conn.run_sync(Base.metadata.create_all, tables=[BIForecastSnapshot.__table__])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _count(db, chart_id="chart1") -> int:
    q = select(BIForecastSnapshot).where(BIForecastSnapshot.chart_id == chart_id)
    return len((await db.execute(q)).scalars().all())


@pytest.mark.asyncio
async def test_upsert_inserts_a_new_snapshot(db):
    store = ForecastSnapshotStore(db)
    await store.upsert(
        chart_id="chart1", owner="alice@example.com", organization_id="acme",
        config_hash="h1", history_end=date(2024, 1, 1), frequency="month",
        payload={"series": []},
    )
    assert await _count(db) == 1


@pytest.mark.asyncio
async def test_upsert_same_key_replaces_instead_of_duplicating(db):
    """Même (chart_id, config_hash, history_end) rejoué deux fois : pas de
    doublon à chaque affichage, mais le payload le plus récent gagne."""
    store = ForecastSnapshotStore(db)
    key = dict(chart_id="chart1", config_hash="h1", history_end=date(2024, 1, 1), frequency="month",
               owner="alice@example.com", organization_id="acme")
    await store.upsert(**key, payload={"series": [{"group": "A"}]})
    await store.upsert(**key, payload={"series": [{"group": "B"}]})

    assert await _count(db) == 1
    rows = await store.list_for_chart("chart1")
    assert rows[0].payload == {"series": [{"group": "B"}]}


@pytest.mark.asyncio
async def test_different_config_hash_is_a_separate_snapshot(db):
    store = ForecastSnapshotStore(db)
    base = dict(chart_id="chart1", history_end=date(2024, 1, 1), frequency="month",
                owner="alice@example.com", organization_id="acme", payload={"series": []})
    await store.upsert(**base, config_hash="h1")
    await store.upsert(**base, config_hash="h2")
    assert await _count(db) == 2


@pytest.mark.asyncio
async def test_retention_keeps_only_the_60_most_recent_per_chart(db):
    store = ForecastSnapshotStore(db)
    for i in range(RETENTION_PER_CHART + 5):
        await store.upsert(
            chart_id="chart1", owner="alice@example.com", organization_id="acme",
            config_hash="h1", history_end=date(2024, 1, 1) + timedelta(days=i),
            frequency="month", payload={"series": []},
        )
    rows = await store.list_for_chart("chart1")
    assert len(rows) == RETENTION_PER_CHART
    # Les plus anciens (history_end les plus petits) ont été supprimés.
    kept_ends = sorted(r.history_end for r in rows)
    assert kept_ends[0] == date(2024, 1, 1) + timedelta(days=5)


@pytest.mark.asyncio
async def test_retention_is_scoped_per_chart(db):
    store = ForecastSnapshotStore(db)
    for i in range(RETENTION_PER_CHART + 2):
        await store.upsert(
            chart_id="chart1", owner="alice@example.com", organization_id="acme",
            config_hash="h1", history_end=date(2024, 1, 1) + timedelta(days=i),
            frequency="month", payload={"series": []},
        )
    await store.upsert(
        chart_id="chart2", owner="alice@example.com", organization_id="acme",
        config_hash="h1", history_end=date(2024, 1, 1), frequency="month",
        payload={"series": []},
    )
    assert await _count(db, "chart1") == RETENTION_PER_CHART
    assert await _count(db, "chart2") == 1


@pytest.mark.asyncio
async def test_list_for_chart_is_empty_for_unknown_chart(db):
    store = ForecastSnapshotStore(db)
    assert await store.list_for_chart("nope") == []

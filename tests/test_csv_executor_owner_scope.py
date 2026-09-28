"""Lot B — CsvQueryExecutor must scope every lookup to the chart's owner.

Before this fix, `_resolve_s3_key` resolved a bare file_id via
`BusinessIntelligence.id` + `type == "data"` with no owner filter, and
accepted any full `bi/data/...` S3 key as-is. A chart whose source pointed
at `csv://<someone else's file_id>` (or their full key) read their CSV.

The owner that must be enforced is the *chart's* owner (`chart.created_by`),
not the reader: a dashboard published by A must stay readable by B, so
`CsvQueryExecutor` is constructed with the chart's owner regardless of who
is viewing.
"""
from __future__ import annotations

import contextlib

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from apowerb.helpers.database import Base
from apowerb.models import BusinessIntelligence
from apowerb.bi.charts.core import Chart, ChartType, DataSource, SourceType
from apowerb.bi.charts.service import ChartService, InMemoryChartStore
from apowerb.bi.data import csv_executor as csv_executor_module
from apowerb.bi.data.csv_executor import CsvQueryExecutor
from apowerb.bi.data.schema import DataRequest
from apowerb.bi.data.service import ChartDataService

OWNER_A = "a@example.com"
OWNER_B = "b@example.com"


class _FakeSessionManager:
    """Mimics `DatabaseSessionManager.session()` against an in-memory DB."""

    def __init__(self, factory):
        self._factory = factory

    @contextlib.asynccontextmanager
    async def session(self):
        async with self._factory() as session:
            yield session


@pytest.fixture
async def db_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    schema = BusinessIntelligence.__table__.schema
    async with engine.begin() as conn:
        if schema:
            await conn.execute(text(f"ATTACH DATABASE ':memory:' AS {schema}"))
        await conn.run_sync(Base.metadata.create_all, tables=[BusinessIntelligence.__table__])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
async def seeded_db(db_factory, monkeypatch):
    """Two datasets, one per owner, each with a distinct S3 key."""
    monkeypatch.setattr(
        "apowerb.helpers.database.sessionmanager", _FakeSessionManager(db_factory)
    )
    async with db_factory() as session:
        session.add(
            BusinessIntelligence(
                id="dataset-a",
                name="a.csv",
                type="data",
                owner=OWNER_A,
                organization_id="org1",
                project_id="thaink2",
                config={"s3_key": "bi/data/org1/thaink2/data/dataset-a.csv"},
            )
        )
        session.add(
            BusinessIntelligence(
                id="dataset-b",
                name="b.csv",
                type="data",
                owner=OWNER_B,
                organization_id="org1",
                project_id="thaink2",
                config={"s3_key": "bi/data/org1/thaink2/data/dataset-b.csv"},
            )
        )
        await session.commit()
    return db_factory


def _fake_read_file(monkeypatch):
    """`read_file` keyed on the two known S3 keys, so a wrong key 404s."""
    files = {
        "bi/data/org1/thaink2/data/dataset-a.csv": b"col\nA-secret\n",
        "bi/data/org1/thaink2/data/dataset-b.csv": b"col\nB-secret\n",
    }

    def fake(key: str):
        return files.get(key)

    monkeypatch.setattr(csv_executor_module, "read_file", fake)


@pytest.mark.asyncio
class TestOwnerScopedResolution:
    async def test_owner_reads_own_csv_by_bare_id(self, seeded_db, monkeypatch):
        _fake_read_file(monkeypatch)
        executor = CsvQueryExecutor(owner=OWNER_A)
        rows = await executor.run(DataSource(query="csv://dataset-a", source_type=SourceType.CSV))
        assert rows == [{"col": "A-secret"}]

    async def test_owner_reads_own_csv_by_full_key(self, seeded_db, monkeypatch):
        _fake_read_file(monkeypatch)
        executor = CsvQueryExecutor(owner=OWNER_A)
        rows = await executor.run(
            DataSource(
                query="csv://bi/data/org1/thaink2/data/dataset-a.csv",
                source_type=SourceType.CSV,
            )
        )
        assert rows == [{"col": "A-secret"}]

    async def test_other_owner_with_bare_id_cannot_read(self, seeded_db, monkeypatch):
        """B lists A's dataset_id — regression test for the vulnerability."""
        _fake_read_file(monkeypatch)
        executor = CsvQueryExecutor(owner=OWNER_B)
        rows = await executor.run(DataSource(query="csv://dataset-a", source_type=SourceType.CSV))
        assert rows == [{"error": "CSV file not found"}]

    async def test_other_owner_with_full_key_cannot_read(self, seeded_db, monkeypatch):
        """B crafts A's full S3 key — regression test for the vulnerability."""
        _fake_read_file(monkeypatch)
        executor = CsvQueryExecutor(owner=OWNER_B)
        rows = await executor.run(
            DataSource(
                query="csv://bi/data/org1/thaink2/data/dataset-a.csv",
                source_type=SourceType.CSV,
            )
        )
        assert rows == [{"error": "CSV file not found"}]

    async def test_unknown_id_and_wrong_owner_give_the_identical_message(self, seeded_db, monkeypatch):
        _fake_read_file(monkeypatch)
        wrong_owner = await CsvQueryExecutor(owner=OWNER_B).run(
            DataSource(query="csv://dataset-a", source_type=SourceType.CSV)
        )
        nonexistent = await CsvQueryExecutor(owner=OWNER_A).run(
            DataSource(query="csv://does-not-exist", source_type=SourceType.CSV)
        )
        assert wrong_owner == nonexistent == [{"error": "CSV file not found"}]


@pytest.mark.asyncio
class TestPublishedDashboardStillReadableByOthers:
    async def test_reader_b_still_sees_owner_a_published_chart_data(self, seeded_db, monkeypatch):
        """The chart's owner (A) must decide access, not the viewer (B):
        a dashboard published by A stays visible for a reader B."""
        _fake_read_file(monkeypatch)
        chart_service = ChartService(InMemoryChartStore())
        chart = Chart.create(
            name="a_chart",
            title="A's chart",
            chart_type=ChartType.BAR,
            source=DataSource(query="csv://dataset-a", source_type=SourceType.CSV),
            created_by=OWNER_A,
            organization_id="org1",
        )
        await chart_service._store.save(chart)

        data_service = ChartDataService(chart_service)
        resp = await data_service.fetch(chart.id, DataRequest(), user_id=OWNER_B)

        assert resp.rows == [{"col": "A-secret"}]


async def _save_chart_row(db_factory, chart_id: str, row_owner: str, created_by):
    """Ligne `chart` telle que l'enregistre DatabaseChartStore : la colonne
    `owner` fait foi, `created_by` n'est qu'un champ de la config."""
    async with db_factory() as session:
        session.add(BusinessIntelligence(
            id=chart_id, name=chart_id, type="chart", owner=row_owner,
            organization_id="org1", project_id="thaink2",
            config={"created_by": created_by},
        ))
        await session.commit()


@pytest.mark.asyncio
class TestChartOwnerComesFromTheStoredRow:
    async def _fetch(self, chart_id, created_by, reader):
        chart_service = ChartService(InMemoryChartStore())
        chart = Chart.create(
            name=chart_id, title=chart_id, chart_type=ChartType.BAR,
            source=DataSource(query="csv://dataset-a", source_type=SourceType.CSV),
            created_by=created_by, organization_id="org1",
        ).model_copy(update={"id": chart_id})
        await chart_service._store.save(chart)
        return await ChartDataService(chart_service).fetch(chart_id, DataRequest(), user_id=reader)

    async def test_chart_without_created_by_still_reads_its_owner_csv(self, seeded_db, monkeypatch):
        """created_by est facultatif : sans lui, le propriétaire de la ligne fait foi."""
        _fake_read_file(monkeypatch)
        await _save_chart_row(seeded_db, "chart-no-creator", OWNER_A, None)
        resp = await self._fetch("chart-no-creator", None, OWNER_B)
        assert resp.rows == [{"col": "A-secret"}]

    async def test_forged_created_by_does_not_grant_access(self, seeded_db, monkeypatch):
        """Un graphique de B dont la config prétend created_by=A ne lit pas le CSV de A."""
        _fake_read_file(monkeypatch)
        await _save_chart_row(seeded_db, "chart-forged", OWNER_B, OWNER_A)
        resp = await self._fetch("chart-forged", OWNER_A, OWNER_B)
        assert resp.rows == [{"error": "CSV file not found"}]


class _FirstSessionFails:
    """La lecture du propriétaire (1re session) échoue, la résolution CSV
    qui suit fonctionne : seul l'échec fermé peut refuser la lecture."""

    def __init__(self, inner):
        self._inner = inner
        self._calls = 0

    def session(self):
        self._calls += 1
        if self._calls == 1:
            raise RuntimeError("database unavailable")
        return self._inner.session()


@pytest.mark.asyncio
async def test_owner_lookup_error_fails_closed(db_factory, seeded_db, monkeypatch):
    """Base illisible au moment du contrôle : created_by=A (forgé) ne suffit pas."""
    _fake_read_file(monkeypatch)
    monkeypatch.setattr(
        "apowerb.helpers.database.sessionmanager",
        _FirstSessionFails(_FakeSessionManager(db_factory)),
    )
    resp = await TestChartOwnerComesFromTheStoredRow()._fetch("chart-db-down", OWNER_A, OWNER_B)
    assert resp.rows == [{"error": "CSV file not found"}]

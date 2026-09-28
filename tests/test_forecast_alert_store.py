"""ForecastAlertStore contre une vraie base (SQLite en memoire) :
dedoublonnage persistant sur (chart_id, group, level, date) -- contrat
etape 7 SS2c. Meme patron que tests/test_forecast_snapshot_store.py."""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from apowerb.bi.forecast_alerts import ForecastAlertStore
from apowerb.helpers.database import Base
from apowerb.models import BIForecastAlert


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    schema = BIForecastAlert.__table__.schema
    async with engine.begin() as conn:
        if schema:
            await conn.execute(text(f"ATTACH DATABASE \':memory:\' AS {schema}"))
        await conn.run_sync(Base.metadata.create_all, tables=[BIForecastAlert.__table__])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_first_reservation_for_a_key_succeeds(db):
    store = ForecastAlertStore(db)
    created = await store.try_reserve(chart_id="chart1", group="A", level=None, date=date(2024, 2, 1))
    assert created is True


@pytest.mark.asyncio
async def test_second_reservation_for_the_same_key_is_deduplicated(db):
    store = ForecastAlertStore(db)
    await store.try_reserve(chart_id="chart1", group="A", level=None, date=date(2024, 2, 1))
    created_again = await store.try_reserve(chart_id="chart1", group="A", level=None, date=date(2024, 2, 1))
    assert created_again is False


@pytest.mark.asyncio
async def test_dedup_key_normalizes_none_group_and_level_so_they_are_not_distinct_nulls(db):
    """Piege SQL : deux NULL ne sont jamais egaux dans une UniqueConstraint
    (Postgres et SQLite) -- (chart1, NULL, NULL, date) s'inserait deux fois
    si group/level restaient NULL en base. Normalise en chaine vide."""
    store = ForecastAlertStore(db)
    await store.try_reserve(chart_id="chart1", group=None, level=None, date=date(2024, 2, 1))
    created_again = await store.try_reserve(chart_id="chart1", group=None, level=None, date=date(2024, 2, 1))
    assert created_again is False
    q = select(BIForecastAlert).where(BIForecastAlert.chart_id == "chart1")
    rows = (await db.execute(q)).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_different_date_is_a_distinct_key(db):
    store = ForecastAlertStore(db)
    await store.try_reserve(chart_id="chart1", group="A", level=None, date=date(2024, 2, 1))
    created = await store.try_reserve(chart_id="chart1", group="A", level=None, date=date(2024, 3, 1))
    assert created is True


@pytest.mark.asyncio
async def test_different_chart_is_a_distinct_key(db):
    store = ForecastAlertStore(db)
    await store.try_reserve(chart_id="chart1", group="A", level=None, date=date(2024, 2, 1))
    created = await store.try_reserve(chart_id="chart2", group="A", level=None, date=date(2024, 2, 1))
    assert created is True


class TestNotifyBreach:
    """notify_breach (contrat etape 7 SS2c) : dedoublonnage + creation de
    Notification pour le proprietaire, sans jamais lever."""

    @pytest.fixture
    async def full_db(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
        schema = BIForecastAlert.__table__.schema
        async with engine.begin() as conn:
            if schema:
                await conn.execute(text(f"ATTACH DATABASE \':memory:\' AS {schema}"))
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            yield session
        await engine.dispose()

    async def _make_user(self, db, email="alice@example.com"):
        from apowerb.models import User, UserRole

        user = User(first_name="Alice", last_name="X", email=email, role=UserRole.USER)
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user

    @pytest.mark.asyncio
    async def test_creates_a_notification_for_the_resolved_owner(self, full_db, monkeypatch):
        from apowerb.bi import forecast_alerts
        from apowerb.models import Notification

        pushed = []
        monkeypatch.setattr(forecast_alerts, "push_notification", lambda uid, payload: pushed.append((uid, payload)) or _noop())

        user = await self._make_user(full_db)
        created = await forecast_alerts.notify_breach(
            full_db, chart_id="chart1", owner_email="ALICE@example.com", group="A", level=None,
            date=date(2024, 2, 1), direction="above", kind="spike", link="/bi/chart1",
        )
        assert created is True
        rows = (await full_db.execute(select(Notification).where(Notification.user_id == user.user_id))).scalars().all()
        assert len(rows) == 1
        assert rows[0].type == "warning"
        assert rows[0].link == "/bi/chart1"
        assert rows[0].title == "Rupture de prévision"
        assert rows[0].message == "La série « A » est au-dessus de la bande prévue le 01/02/2024."

    @pytest.mark.asyncio
    async def test_second_call_for_the_same_breach_does_not_notify_again(self, full_db, monkeypatch):
        from apowerb.bi import forecast_alerts
        from apowerb.models import Notification

        monkeypatch.setattr(forecast_alerts, "push_notification", lambda uid, payload: _noop())
        await self._make_user(full_db)
        kwargs = dict(
            chart_id="chart1", owner_email="alice@example.com", group="A", level=None,
            date=date(2024, 2, 1), direction="above", kind="spike", link="/bi/chart1",
        )
        first = await forecast_alerts.notify_breach(full_db, **kwargs)
        second = await forecast_alerts.notify_breach(full_db, **kwargs)
        assert first is True
        assert second is False
        rows = (await full_db.execute(select(Notification))).scalars().all()
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_unknown_owner_does_not_raise_and_returns_false(self, full_db, monkeypatch):
        from apowerb.bi import forecast_alerts

        monkeypatch.setattr(forecast_alerts, "push_notification", lambda uid, payload: _noop())
        created = await forecast_alerts.notify_breach(
            full_db, chart_id="chart1", owner_email="ghost@example.com", group="A", level=None,
            date=date(2024, 2, 1), direction="above", kind="spike", link="/bi/chart1",
        )
        assert created is False


async def _noop():
    return None


class TestReservationIsAtomicWithTheNotification:
    """Correctifs de relecture : une alerte n'est marquee vue que si sa
    notification a bien ete enregistree."""

    @pytest.fixture
    async def full_db(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
        # pysqlite n'ouvre pas de transaction avant un SAVEPOINT : sans ces
        # deux ecouteurs (recette de la doc SQLAlchemy), le point de sauvegarde
        # se valide seul et ce test ne verrait pas ce que Postgres garantit.
        event.listen(engine.sync_engine, "connect", lambda dbapi_conn, _rec: setattr(dbapi_conn, "isolation_level", None))
        event.listen(engine.sync_engine, "begin", lambda conn: conn.exec_driver_sql("BEGIN"))
        schema = BIForecastAlert.__table__.schema
        async with engine.begin() as conn:
            if schema:
                await conn.execute(text(f"ATTACH DATABASE ':memory:' AS {schema}"))
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            yield session
        await engine.dispose()

    async def _make_user(self, db, email="alice@example.com"):
        from apowerb.models import User, UserRole

        user = User(first_name="Alice", last_name="X", email=email, role=UserRole.USER)
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user

    _KW = dict(chart_id="chart1", owner_email="alice@example.com", group="A", level="80",
               date=date(2024, 2, 1), direction="above", kind="spike", link="/bi/d1")

    @pytest.mark.asyncio
    async def test_unknown_owner_does_not_consume_the_reservation(self, full_db, monkeypatch):
        from apowerb.bi import forecast_alerts

        monkeypatch.setattr(forecast_alerts, "push_notification", lambda uid, payload: _noop())
        assert await forecast_alerts.notify_breach(full_db, **self._KW) is False
        await self._make_user(full_db)
        assert await forecast_alerts.notify_breach(full_db, **self._KW) is True

    @pytest.mark.asyncio
    async def test_failed_notification_commit_releases_the_reservation(self, full_db, monkeypatch):
        from apowerb.bi import forecast_alerts

        monkeypatch.setattr(forecast_alerts, "push_notification", lambda uid, payload: _noop())
        await self._make_user(full_db)
        real_commit = full_db.commit

        from apowerb.models import Notification

        async def failing_commit():
            # Echoue seulement sur le commit qui porte la notification : un
            # commit anterieur de la seule reservation passerait (ancien bug).
            if any(isinstance(o, Notification) for o in full_db.new):
                raise RuntimeError("db down")
            await real_commit()

        monkeypatch.setattr(full_db, "commit", failing_commit)
        with pytest.raises(RuntimeError):
            await forecast_alerts.notify_breach(full_db, **self._KW)
        await full_db.rollback()
        monkeypatch.setattr(full_db, "commit", real_commit)
        assert (await full_db.execute(select(BIForecastAlert))).scalars().all() == []
        assert await forecast_alerts.notify_breach(full_db, **self._KW) is True


class TestDashboardLinkForChart:
    @pytest.fixture
    async def full_db(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
        schema = BIForecastAlert.__table__.schema
        async with engine.begin() as conn:
            if schema:
                await conn.execute(text(f"ATTACH DATABASE ':memory:' AS {schema}"))
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            yield session
        await engine.dispose()

    @pytest.mark.asyncio
    async def test_links_to_the_dashboard_that_shows_the_chart(self, full_db):
        from apowerb.bi.dashboards.core import Dashboard, DashboardComponent
        from apowerb.bi.db_stores import DatabaseDashboardStore
        from apowerb.bi.forecast_alerts import dashboard_link_for_chart

        store = DatabaseDashboardStore(full_db, owner="alice@example.com")
        await store.save(Dashboard.create(title="autre", components=[DashboardComponent.from_chart("chart-x")]))
        target = await store.save(Dashboard.create(title="ventes", components=[DashboardComponent.from_chart("chart1")]))

        assert await dashboard_link_for_chart(full_db, owner="alice@example.com", chart_id="chart1") == f"/bi/{target.id}"

    @pytest.mark.asyncio
    async def test_falls_back_to_the_bi_home_when_no_dashboard_shows_it(self, full_db):
        from apowerb.bi.forecast_alerts import dashboard_link_for_chart

        assert await dashboard_link_for_chart(full_db, owner="alice@example.com", chart_id="chart1") == "/bi"

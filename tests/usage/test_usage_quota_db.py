"""Quota aggregation against a real database (SQLite in memory).

What these tests protect, and no unit test can see: the query's three
filters. Forgetting one would bill a user for someone else's consumption,
for their own API key's consumption, or for last month's.
"""
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from apowerb.core import usage_quota as uq
from apowerb.helpers.database import Base
from apowerb.models import LlmUsage


class _FakeSettings:
    default_llm_monthly_token_quota = 1000
    default_llm_plan_quotas: dict = {}


@pytest.fixture
async def db(monkeypatch) -> AsyncIterator:
    monkeypatch.setattr(uq, "get_settings", lambda: _FakeSettings())
    # The project's MetaData carries `schema=<DB_SCHEMA>` (helpers/database.py),
    # so tables are emitted as `<schema>.llm_usage`. SQLite refuses an
    # unknown schema ("unknown database public") -- hence the ATTACH, which
    # makes it accept this name as a secondary database. StaticPool keeps a
    # single connection, otherwise the ATTACH would only apply to the first one.
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool
    )
    schema = LlmUsage.__table__.schema
    async with engine.begin() as conn:
        if schema:
            await conn.execute(text(f"ATTACH DATABASE ':memory:' AS {schema}"))
        # Seulement llm_usage : ce test n'a besoin d'aucune autre table.
        await conn.run_sync(Base.metadata.create_all, tables=[LlmUsage.__table__])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _add(db, **kw):
    defaults = dict(
        agent_id=1,
        agent_name="a",
        owner_id="dave@example.com",
        model="gemini/gemini-2.5-flash",
        billed_to_thaink2=True,
        total_tokens=100,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    db.add(LlmUsage(**defaults))
    await db.commit()


@pytest.mark.asyncio
async def test_sums_the_month_for_this_owner(db):
    await _add(db, total_tokens=100)
    await _add(db, total_tokens=250)
    st = await uq.get_quota_status(db, owner_id="dave@example.com", plan="free")
    assert st.used_tokens == 350
    assert st.remaining_tokens == 650


@pytest.mark.asyncio
async def test_another_owner_is_excluded(db):
    await _add(db, total_tokens=100)
    await _add(db, owner_id="someone@else.com", total_tokens=9000)
    st = await uq.get_quota_status(db, owner_id="dave@example.com", plan="free")
    assert st.used_tokens == 100


@pytest.mark.asyncio
async def test_personal_key_usage_is_excluded(db):
    """La consommation sur une cle perso est payee par l'utilisateur : la
    compter dans le quota mutualise le penaliserait deux fois."""
    await _add(db, total_tokens=100, billed_to_thaink2=True)
    await _add(db, total_tokens=9000, billed_to_thaink2=False)
    st = await uq.get_quota_status(db, owner_id="dave@example.com", plan="free")
    assert st.used_tokens == 100


@pytest.mark.asyncio
async def test_previous_month_is_excluded(db):
    last_month = uq.month_start() - timedelta(minutes=1)
    await _add(db, total_tokens=9000, created_at=last_month)
    await _add(db, total_tokens=100)
    st = await uq.get_quota_status(db, owner_id="dave@example.com", plan="free")
    assert st.used_tokens == 100


@pytest.mark.asyncio
async def test_the_very_first_row_of_the_month_counts(db):
    """Borne inferieure inclusive : une ligne ecrite a la seconde du
    basculement appartient au nouveau mois."""
    await _add(db, total_tokens=100, created_at=uq.month_start())
    st = await uq.get_quota_status(db, owner_id="dave@example.com", plan="free")
    assert st.used_tokens == 100


@pytest.mark.asyncio
async def test_no_usage_at_all_is_zero_not_an_error(db):
    st = await uq.get_quota_status(db, owner_id="personne@example.com", plan="free")
    assert st.used_tokens == 0
    assert not st.exceeded


@pytest.mark.asyncio
async def test_crossing_the_limit_flips_exceeded(db):
    await _add(db, total_tokens=999)
    assert not (await uq.get_quota_status(db, owner_id="dave@example.com", plan="free")).exceeded
    await _add(db, total_tokens=1)
    assert (await uq.get_quota_status(db, owner_id="dave@example.com", plan="free")).exceeded

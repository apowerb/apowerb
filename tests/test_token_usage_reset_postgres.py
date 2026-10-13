"""Remise a zero du compteur de jetons sur un vrai PostgreSQL.

SQLite ne peut pas prouver ce que le savepoint de ``usage_counted_since``
protege : sur PostgreSQL, une requete en echec AVORTE la transaction en cours,
et toute requete suivante sur la meme session echoue
(``InFailedSQLTransactionError``). SQLite, lui, continue.

Lancer avec ``TEST_POSTGRES_URL=postgresql+asyncpg://user@hote:port/une_base``.
Le test cree sa propre base ``reset_usage_<uuid>`` sur ce serveur, y travaille
et la supprime : la base nommee dans l'URL n'est jamais modifiee. Sans la
variable, tout est ignore.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from apowerb.core.usage_quota import get_quota_status
from apowerb.helpers.database import Base
from apowerb.models import LlmUsage, User

POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not POSTGRES_URL, reason="TEST_POSTGRES_URL non defini : PostgreSQL requis"
)

MOI = "pg-reset@exemple.fr"
AUTRE = "pg-autre@exemple.fr"


@pytest.fixture
async def factory():
    admin_engine = create_async_engine(POSTGRES_URL, isolation_level="AUTOCOMMIT")
    nom = f"reset_usage_{uuid.uuid4().hex[:12]}"
    async with admin_engine.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{nom}"'))

    engine = create_async_engine(make_url(POSTGRES_URL).set(database=nom))
    try:
        async with engine.begin() as conn:
            await conn.run_sync(
                Base.metadata.create_all,
                tables=[User.__table__, LlmUsage.__table__],
            )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as db:
            db.add(User(user_id=1, email=MOI, first_name="P", last_name="G"))
            db.add(User(user_id=2, email=AUTRE, first_name="A", last_name="B"))
            await db.commit()
        yield sessions
    finally:
        await engine.dispose()
        async with admin_engine.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{nom}" WITH (FORCE)'))
        await admin_engine.dispose()


async def _consomme(factory, owner: str, jetons: int, il_y_a_min: float) -> None:
    async with factory() as db:
        db.add(
            LlmUsage(
                created_at=datetime.now(timezone.utc) - timedelta(minutes=il_y_a_min),
                agent_id=1,
                agent_name="agent",
                owner_id=owner,
                model="gemini-2.5-flash",
                input_tokens=jetons,
                output_tokens=0,
                thoughts_tokens=0,
                cached_tokens=0,
                total_tokens=jetons,
                billed_to_thaink2=True,
            )
        )
        await db.commit()


async def _utilise(factory, owner: str) -> int:
    async with factory() as db:
        return (await get_quota_status(db, owner_id=owner, plan=None)).used_tokens


async def _coupe(factory, owner: str, il_y_a_min: float = 0) -> None:
    async with factory() as db:
        user = (await db.execute(select(User).where(User.email == owner))).scalar_one()
        user.token_usage_reset_at = datetime.now(timezone.utc) - timedelta(minutes=il_y_a_min)
        await db.commit()


async def test_postgres_la_coupure_fonctionne(factory):
    await _consomme(factory, MOI, 300, 30)
    await _consomme(factory, AUTRE, 700, 30)
    assert await _utilise(factory, MOI) == 300

    await _coupe(factory, MOI, il_y_a_min=10)
    await _consomme(factory, MOI, 200, 5)

    assert await _utilise(factory, MOI) == 200
    assert await _utilise(factory, AUTRE) == 700


async def test_postgres_une_coupure_posee_en_timestamptz_n_est_pas_decalee(factory):
    """Postgres renvoie des datetimes aware ; la comparaison reste exacte a la
    minute pres de part et d'autre de la coupure."""
    await _consomme(factory, MOI, 111, 20)
    await _coupe(factory, MOI, il_y_a_min=10)
    await _consomme(factory, MOI, 22, 9)
    await _consomme(factory, MOI, 3, 11)  # avant la coupure : exclu
    assert await _utilise(factory, MOI) == 22


async def _supprime_la_colonne(factory) -> None:
    async with factory() as db:
        await db.execute(text('ALTER TABLE "user" DROP COLUMN token_usage_reset_at'))
        await db.commit()


async def test_postgres_sans_la_colonne_tout_le_mois_compte_et_la_session_survit(factory):
    await _consomme(factory, MOI, 300, 30)
    await _consomme(factory, MOI, 40, 5)
    await _supprime_la_colonne(factory)

    async with factory() as db:
        # La session a deja une transaction ouverte : c'est elle que la
        # lecture en echec menace d'avorter.
        assert (await db.execute(text("SELECT 1"))).scalar() == 1

        statut = await get_quota_status(db, owner_id=MOI, plan=None)
        assert statut.used_tokens == 340, "sans coupure lisible, tout le mois doit compter"

        assert db.in_nested_transaction() is False
        assert (await db.execute(text("SELECT 2"))).scalar() == 2
        total = (await db.execute(select(text("count(*)")).select_from(LlmUsage))).scalar()
        assert total == 2


async def test_postgres_sans_savepoint_la_transaction_serait_avortee(factory):
    """Temoin : la MEME lecture, hors ``begin_nested``, avorte la transaction.

    Il fait de l'essai precedent un vrai discriminant : si cette lecture ne
    cassait pas la session, le savepoint ne prouverait rien.
    """
    await _consomme(factory, MOI, 300, 30)
    await _supprime_la_colonne(factory)

    async with factory() as db:
        assert (await db.execute(text("SELECT 1"))).scalar() == 1
        with pytest.raises(DBAPIError):
            await db.execute(
                select(User.token_usage_reset_at).where(User.email == MOI)
            )
        with pytest.raises(DBAPIError) as suivante:
            await db.execute(text("SELECT 2"))
        assert "InFailedSQLTransaction" in repr(suivante.value) or "current transaction is aborted" in str(
            suivante.value
        )

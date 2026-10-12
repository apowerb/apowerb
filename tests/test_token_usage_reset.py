"""Remise a zero du compteur de jetons d'un compte, par un administrateur.

Rien n'est efface : ``llm_usage`` reste l'historique de consommation. La
remise a zero est une COUPURE posee sur le compte (``token_usage_reset_at``),
et chaque lecteur du compteur la respecte -- la garde mensuelle, la jauge
que voit l'utilisateur et le plafond glissant. Si un seul l'ignorait, la
jauge annoncerait un chiffre et la garde en appliquerait un autre.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apowerb.admin import router as admin_router
from apowerb.configs.settings import get_settings
from apowerb.core import run_gate
from apowerb.core.agent_helpers.default_llm_usage import used_tokens_this_month
from apowerb.core.extensions.registry import registry
from apowerb.core.usage_quota import get_quota_status
from apowerb.helpers.database import Base, sessionmanager
from apowerb.models import LlmUsage, User

MOI = "eval@exemple.fr"


@pytest.fixture
def registre_vierge(monkeypatch):
    monkeypatch.setattr(registry, "_run_guards", [], raising=False)
    monkeypatch.setattr(registry, "_default_llm_cap", None, raising=False)
    return registry


@pytest.fixture
async def base(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    for table in Base.metadata.tables.values():
        monkeypatch.setattr(table, "schema", None)
    monkeypatch.setattr(Base.metadata, "schema", None)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def _session() -> AsyncIterator[AsyncSession]:
        session = factory()
        try:
            yield session
        finally:
            await session.close()

    monkeypatch.setattr(sessionmanager, "session", _session, raising=False)
    async with factory() as db:
        db.add(User(user_id=7, email=MOI, first_name="E", last_name="G"))
        await db.commit()
    yield factory
    await engine.dispose()


async def consomme(factory, *, jetons: int, il_y_a_min: float) -> None:
    async with factory() as db:
        db.add(
            LlmUsage(
                created_at=datetime.now(timezone.utc) - timedelta(minutes=il_y_a_min),
                agent_id=1,
                agent_name="agent",
                owner_id=MOI,
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


async def remet_a_zero(factory, il_y_a_min: float) -> None:
    async with factory() as db:
        user = (await db.execute(select(User).where(User.email == MOI))).scalar_one()
        user.token_usage_reset_at = datetime.now(timezone.utc) - timedelta(minutes=il_y_a_min)
        await db.commit()


async def compteurs(factory) -> tuple[int, int]:
    """(garde mensuelle, jauge) -- les deux doivent toujours concorder."""
    async with factory() as db:
        garde = (await get_quota_status(db, owner_id=MOI, plan=None)).used_tokens
        jauge = await used_tokens_this_month(db, MOI)
    return garde, jauge


@pytest.mark.asyncio
async def test_sans_remise_a_zero_tout_le_mois_compte(base):
    await consomme(base, jetons=300, il_y_a_min=30)
    await consomme(base, jetons=200, il_y_a_min=5)
    assert await compteurs(base) == (500, 500)


@pytest.mark.asyncio
async def test_apres_remise_a_zero_seule_la_suite_compte(base):
    await consomme(base, jetons=300, il_y_a_min=30)
    await remet_a_zero(base, il_y_a_min=10)
    await consomme(base, jetons=200, il_y_a_min=5)
    assert await compteurs(base) == (200, 200)


@pytest.mark.asyncio
async def test_une_remise_a_zero_du_mois_dernier_ne_rouvre_rien(base):
    """La coupure ne recule jamais le debut du mois."""
    await remet_a_zero(base, il_y_a_min=60 * 24 * 40)
    await consomme(base, jetons=300, il_y_a_min=30)
    assert await compteurs(base) == (300, 300)


@pytest.mark.asyncio
async def test_le_plafond_glissant_repart_de_zero(base, registre_vierge, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "default_llm_user_token_cap", 1_000, raising=False)
    monkeypatch.setattr(s, "default_llm_global_token_cap", 0, raising=False)
    monkeypatch.setattr(s, "default_llm_cap_window_hours", 24, raising=False)

    await consomme(base, jetons=1_000, il_y_a_min=30)
    with pytest.raises(HTTPException) as leve:
        await run_gate.apply_run_guards(agent_name="agent6", owner_id=MOI, plan=None)
    assert leve.value.status_code == 402

    await remet_a_zero(base, il_y_a_min=1)
    await run_gate.apply_run_guards(agent_name="agent6", owner_id=MOI, plan=None)


@pytest.mark.asyncio
async def test_la_route_admin_pose_la_coupure_sans_rien_effacer(base, monkeypatch):
    await consomme(base, jetons=300, il_y_a_min=30)

    async def autorise(db, admin, user_id):
        return None

    monkeypatch.setattr(admin_router, "_assert_may_act_on", autorise)
    admin = MagicMock(role="ADMIN", email="boss@exemple.fr")

    avant = datetime.now(timezone.utc)
    async with base() as db:
        out = await admin_router.reset_token_usage(7, db=db, _=admin)
    assert out.email == MOI

    async with base() as db:
        user = (await db.execute(select(User).where(User.user_id == 7))).scalar_one()
        coupure = user.token_usage_reset_at
        lignes = (await db.execute(select(LlmUsage))).scalars().all()
    assert coupure is not None
    coupure = coupure if coupure.tzinfo else coupure.replace(tzinfo=timezone.utc)
    assert coupure >= avant - timedelta(seconds=1)
    assert len(lignes) == 1, "l'historique de consommation ne doit pas etre efface"
    assert await compteurs(base) == (0, 0)


@pytest.mark.asyncio
async def test_la_route_admin_refuse_un_compte_inconnu(base, monkeypatch):
    async def autorise(db, admin, user_id):
        return None

    monkeypatch.setattr(admin_router, "_assert_may_act_on", autorise)
    async with base() as db:
        with pytest.raises(HTTPException) as leve:
            await admin_router.reset_token_usage(999, db=db, _=MagicMock())
    assert leve.value.status_code == 404


@pytest.mark.asyncio
async def test_une_coupure_illisible_garde_le_compteur_et_la_session(base):
    """Sans la colonne (migration ratee), on compte tout le mois -- et la
    session reste utilisable : sur Postgres, une requete en echec avorte la
    transaction, et la somme qui suit tomberait avec, emportant le quota."""
    from sqlalchemy import text

    await consomme(base, jetons=300, il_y_a_min=30)
    async with base() as db:
        await db.execute(text('ALTER TABLE "user" DROP COLUMN token_usage_reset_at'))
        await db.commit()

    async with base() as db:
        assert (await get_quota_status(db, owner_id=MOI, plan=None)).used_tokens == 300
        assert db.in_nested_transaction() is False

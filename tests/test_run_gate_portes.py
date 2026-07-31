"""Le portier de run et la fermeture effective du chemin webhook.

Le test de couverture voisin prouve que chaque module APPELLE le portier.
Celui-ci prouve ce qui compte vraiment : quand une garde refuse, le run
n'a pas lieu -- l'agent n'est jamais atteint.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from apowerb.core import run_gate
from apowerb.core.extensions.registry import registry
from apowerb.helpers.database import Base
from apowerb.models import User
from apowerb.routers.webhook_handlers import _common


@pytest.fixture
def registre_vierge(monkeypatch):
    """Isole les gardes : un test ne doit pas voir celles d'un autre."""
    monkeypatch.setattr(registry, "_run_guards", [], raising=False)
    return registry


class TestPortier:
    @pytest.mark.asyncio
    async def test_sans_brique_installee_rien_ne_bloque(self, registre_vierge):
        await run_gate.apply_run_guards(
            agent_name="agent6", owner_id="a@b.fr", plan="free"
        )

    @pytest.mark.asyncio
    async def test_transmet_agent_proprietaire_et_plan(self, registre_vierge):
        vus: list[tuple] = []

        async def garde(agent_name, *, owner_id, plan):
            vus.append((agent_name, owner_id, plan))

        registre_vierge.register_run_guard(garde)

        await run_gate.apply_run_guards(
            agent_name="agent6", owner_id="com@scei88.fr", plan="pro"
        )

        assert vus == [("agent6", "com@scei88.fr", "pro")]

    @pytest.mark.asyncio
    async def test_refus_de_garde_remonte(self, registre_vierge):
        async def garde(agent_name, *, owner_id, plan):
            raise HTTPException(status_code=402, detail="quota")

        registre_vierge.register_run_guard(garde)

        with pytest.raises(HTTPException) as leve:
            await run_gate.apply_run_guards(
                agent_name="agent6", owner_id="a@b.fr", plan=None
            )
        assert leve.value.status_code == 402

    @pytest.mark.asyncio
    async def test_proprietaire_absent_laisse_passer_mais_previent(
        self, registre_vierge, caplog
    ):
        """Marche ouverte, mais bruyante.

        Un plafond commercial ne doit pas rendre le produit muet quand
        l'identite est irresolvable. En revanche l'ouverture doit
        s'entendre, sinon elle redevient le trou qu'on vient de fermer.
        """
        appelee = False

        async def garde(agent_name, *, owner_id, plan):
            nonlocal appelee
            appelee = True

        registre_vierge.register_run_guard(garde)

        with caplog.at_level("WARNING"):
            await run_gate.apply_run_guards(
                agent_name="agent6", owner_id="", plan=None
            )

        assert not appelee
        assert any("no owner" in m for m in caplog.messages), (
            "un run non plafonne faute d'identite doit laisser une trace"
        )


@pytest.fixture
async def sqlite_avec_utilisateur(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    for table in Base.metadata.tables.values():
        table.schema = None
    Base.metadata.schema = None
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        db.add(
            User(
                user_id=3,
                first_name="Commerciale",
                last_name="SCEI",
                email="com@scei88.fr",
                password="x",
                role="USER",
                plan="free",
            )
        )
        await db.commit()

    @asynccontextmanager
    async def _session() -> AsyncIterator[AsyncSession]:
        session = factory()
        try:
            yield session
        finally:
            await session.close()

    monkeypatch.setattr(_common.sessionmanager, "session", _session, raising=False)
    yield factory
    await engine.dispose()


class TestPorteWebhook:
    """Le chemin webhook atteint le /run natif d'ADK : avant ce correctif,
    aucune garde ne s'y appliquait et le plafond se contournait en
    declenchant l'agent par webhook plutot que depuis le chat."""

    @pytest.mark.asyncio
    async def test_quota_depasse_empeche_le_run(
        self, sqlite_avec_utilisateur, registre_vierge, monkeypatch
    ):
        agent_atteint = False

        async def garde(agent_name, *, owner_id, plan):
            raise HTTPException(status_code=402, detail={"code": "QUOTA_EXCEEDED"})

        registre_vierge.register_run_guard(garde)

        async def faux_get(*, agent_name, user_id, session_id, token):
            return {"id": session_id}

        async def faux_run(**kwargs):
            nonlocal agent_atteint
            agent_atteint = True
            return [{"content": {"parts": [{"text": "ok"}]}}]

        monkeypatch.setattr(_common, "get_adk_session", faux_get)
        monkeypatch.setattr(_common, "run_adk_agent", faux_run)

        with pytest.raises(HTTPException) as leve:
            await _common.run_agent_for_webhook(
                user_id=3,
                agent_id=6,
                sub_db_id=1,
                session_id="webhook_1",
                message_text="bonjour",
            )

        assert leve.value.status_code == 402
        assert not agent_atteint, (
            "le run a eu lieu malgre le refus : le plafond reste contournable "
            "par webhook"
        )

    @pytest.mark.asyncio
    async def test_la_garde_recoit_le_plan_du_proprietaire(
        self, sqlite_avec_utilisateur, registre_vierge, monkeypatch
    ):
        vus: list[tuple] = []

        async def garde(agent_name, *, owner_id, plan):
            vus.append((agent_name, owner_id, plan))

        registre_vierge.register_run_guard(garde)

        async def faux_get(*, agent_name, user_id, session_id, token):
            return {"id": session_id}

        async def faux_run(**kwargs):
            return [{"content": {"parts": [{"text": "ok"}]}}]

        monkeypatch.setattr(_common, "get_adk_session", faux_get)
        monkeypatch.setattr(_common, "run_adk_agent", faux_run)

        await _common.run_agent_for_webhook(
            user_id=3,
            agent_id=6,
            sub_db_id=1,
            session_id="webhook_1",
            message_text="bonjour",
        )

        assert vus == [("agent6", "com@scei88.fr", "free")], (
            "la garde doit connaitre le proprietaire reel et son plan, sinon "
            "elle plafonne le mauvais compte"
        )

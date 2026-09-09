"""Le plafond de jetons que l'OSS applique tout seul.

Jusqu'ici le noyau COMPTAIT la consommation du modele mutualise sans jamais
la plafonner : `apply_run_guards` sortait immediatement faute de garde
enregistree, et `DEFAULT_LLM_MONTHLY_TOKEN_QUOTA` n'avait aucun site d'appel.
Mesure du 09/09 sur la demo : `/api/config` rend `default_llm_available: true`
sans authentification, et l'inscription est ouverte -- donc un modele
mutualise sans aucune limite, offert a qui passe.

Ce garde vit dans le noyau, pas dans une brique : c'est la difference entre
un plafond installable et un plafond installe.

Deux plafonds, tous deux sur la seule consommation `billed_to_thaink2` --
une cle API personnelle est payee par son proprietaire, la plafonner serait
une faute :
  * par utilisateur, sur une fenetre GLISSANTE. Une remise a zero calendaire
    laisserait prendre 1 M a 23h59 et 1 M a 00h01.
  * global au deploiement, parce qu'un plafond par compte ne vaut que si le
    nombre de comptes est borne -- et l'inscription est ouverte.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from apowerb.configs.settings import get_settings
from apowerb.core import run_gate
from apowerb.core.extensions.registry import registry
from apowerb.helpers.database import Base, sessionmanager
from apowerb.models import LlmUsage

MOI = "eval@exemple.fr"
QUELQU_UN_DAUTRE = "autre@exemple.fr"


@pytest.fixture
def registre_vierge(monkeypatch):
    monkeypatch.setattr(registry, "_run_guards", [], raising=False)
    monkeypatch.setattr(registry, "_default_llm_cap", None, raising=False)
    return registry


@pytest.fixture
async def base(monkeypatch):
    """Une base reelle : le garde se juge sur une requete, pas sur un mock."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    for table in Base.metadata.tables.values():
        table.schema = None
    Base.metadata.schema = None
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
    yield factory
    await engine.dispose()


async def consomme(
    factory, *, owner: str, jetons: int, il_y_a_h: float = 0.0, mutualise: bool = True
) -> None:
    async with factory() as db:
        db.add(
            LlmUsage(
                created_at=datetime.now(timezone.utc) - timedelta(hours=il_y_a_h),
                agent_id=1,
                agent_name="agent",
                owner_id=owner,
                model="gemini-2.5-flash",
                input_tokens=jetons,
                output_tokens=0,
                thoughts_tokens=0,
                cached_tokens=0,
                total_tokens=jetons,
                billed_to_thaink2=mutualise,
            )
        )
        await db.commit()


def regle(monkeypatch, *, par_utilisateur=1_000_000, global_=20_000_000, fenetre=24):
    s = get_settings()
    monkeypatch.setattr(s, "default_llm_user_token_cap", par_utilisateur, raising=False)
    monkeypatch.setattr(s, "default_llm_global_token_cap", global_, raising=False)
    monkeypatch.setattr(s, "default_llm_cap_window_hours", fenetre, raising=False)


async def porte(owner=MOI):
    await run_gate.apply_run_guards(agent_name="agent6", owner_id=owner, plan=None)


class TestPlafondParUtilisateur:
    @pytest.mark.asyncio
    async def test_sous_le_plafond_passe(self, base, registre_vierge, monkeypatch):
        regle(monkeypatch)
        await consomme(base, owner=MOI, jetons=999_999)
        await porte()

    @pytest.mark.asyncio
    async def test_au_plafond_refuse_en_402(self, base, registre_vierge, monkeypatch):
        regle(monkeypatch)
        await consomme(base, owner=MOI, jetons=1_000_000)
        with pytest.raises(HTTPException) as leve:
            await porte()
        assert leve.value.status_code == 402

    @pytest.mark.asyncio
    async def test_le_refus_dit_quand_ca_recharge(
        self, base, registre_vierge, monkeypatch
    ):
        """Un mur sans heure de reouverture est un mur qui n'apprend rien."""
        regle(monkeypatch)
        await consomme(base, owner=MOI, jetons=1_200_000, il_y_a_h=3)
        with pytest.raises(HTTPException) as leve:
            await porte()
        detail = leve.value.detail
        assert isinstance(detail, dict), "le detail doit etre lisible par l'interface"
        assert detail.get("code") == "TOKEN_QUOTA_EXCEEDED"
        assert detail.get("retry_after_seconds", 0) > 0

    @pytest.mark.asyncio
    async def test_la_consommation_d_un_autre_ne_me_bloque_pas(
        self, base, registre_vierge, monkeypatch
    ):
        regle(monkeypatch, global_=0)
        await consomme(base, owner=QUELQU_UN_DAUTRE, jetons=5_000_000)
        await porte()


class TestFenetreGlissante:
    @pytest.mark.asyncio
    async def test_hors_fenetre_ne_compte_plus(
        self, base, registre_vierge, monkeypatch
    ):
        regle(monkeypatch)
        await consomme(base, owner=MOI, jetons=1_000_000, il_y_a_h=25)
        await porte()

    @pytest.mark.asyncio
    async def test_dans_la_fenetre_compte_encore(
        self, base, registre_vierge, monkeypatch
    ):
        regle(monkeypatch)
        await consomme(base, owner=MOI, jetons=1_000_000, il_y_a_h=23)
        with pytest.raises(HTTPException):
            await porte()

    @pytest.mark.asyncio
    async def test_pas_de_rafale_de_minuit(self, base, registre_vierge, monkeypatch):
        """Le defaut qu'une remise a zero calendaire aurait laisse passer :
        deux pleins consecutifs a quelques minutes d'intervalle."""
        regle(monkeypatch)
        await consomme(base, owner=MOI, jetons=600_000, il_y_a_h=0.1)
        await consomme(base, owner=MOI, jetons=600_000, il_y_a_h=0.05)
        with pytest.raises(HTTPException):
            await porte()


class TestCeQuiCompte:
    @pytest.mark.asyncio
    async def test_une_cle_perso_n_est_pas_plafonnee(
        self, base, registre_vierge, monkeypatch
    ):
        """`billed_to_thaink2=False` : consommation payee par son proprietaire."""
        regle(monkeypatch)
        await consomme(base, owner=MOI, jetons=9_000_000, mutualise=False)
        await porte()


class TestPlafondGlobal:
    @pytest.mark.asyncio
    async def test_le_deploiement_entier_est_borne(
        self, base, registre_vierge, monkeypatch
    ):
        """Un plafond par compte ne vaut rien si les comptes sont illimites."""
        regle(monkeypatch, global_=2_000_000)
        await consomme(base, owner=QUELQU_UN_DAUTRE, jetons=1_500_000)
        await consomme(base, owner="troisieme@exemple.fr", jetons=600_000)
        with pytest.raises(HTTPException) as leve:
            await porte()
        assert leve.value.detail.get("scope") == "deployment"

    @pytest.mark.asyncio
    async def test_zero_desactive_le_plafond(
        self, base, registre_vierge, monkeypatch
    ):
        regle(monkeypatch, par_utilisateur=0, global_=0)
        await consomme(base, owner=MOI, jetons=50_000_000)
        await porte()


class TestArticulationAvecLeCommercial:
    @pytest.mark.asyncio
    async def test_une_brique_de_plafond_prend_la_main(
        self, base, registre_vierge, monkeypatch
    ):
        """Le crochet DEDIE au plafond, pas n'importe quelle garde : une
        extension sans rapport ne doit pas desarmer le plafond du noyau."""
        regle(monkeypatch)
        await consomme(base, owner=MOI, jetons=5_000_000)

        async def plafond_commercial(db, *, owner_id, plan):
            return None

        registre_vierge.register_default_llm_cap(plafond_commercial)
        await porte()

    @pytest.mark.asyncio
    async def test_une_garde_sans_rapport_ne_desarme_rien(
        self, base, registre_vierge, monkeypatch
    ):
        regle(monkeypatch)
        await consomme(base, owner=MOI, jetons=5_000_000)

        async def garde_sans_rapport(agent_name, *, owner_id, plan):
            return None

        registre_vierge.register_run_guard(garde_sans_rapport)
        with pytest.raises(HTTPException):
            await porte()


class TestPannesDeLecture:
    @pytest.mark.asyncio
    async def test_base_illisible_laisse_passer_mais_previent(
        self, base, registre_vierge, monkeypatch, caplog
    ):
        """Meme regle que le reste de ce module : on n'ouvre pas en silence.

        Rendre le produit muet sur un hoquet de base serait pire que le
        depassement qu'on evite -- mais l'ouverture doit s'entendre.
        """
        regle(monkeypatch)

        @asynccontextmanager
        async def _casse():
            raise RuntimeError("base injoignable")
            yield  # pragma: no cover

        monkeypatch.setattr(sessionmanager, "session", _casse, raising=False)

        with caplog.at_level("WARNING"):
            await porte()

        assert any(
            "plafond" in m.lower() or "cap" in m.lower() for m in caplog.messages
        ), "une ouverture faute de lecture doit laisser une trace"

    @pytest.mark.asyncio
    async def test_sans_proprietaire_le_plafond_global_s_applique_quand_meme(
        self, base, registre_vierge, monkeypatch
    ):
        """Un run sans identite echappait a tout. Le plafond du deploiement,
        lui, ne depend d'aucune identite."""
        regle(monkeypatch, global_=1_000_000)
        await consomme(base, owner=QUELQU_UN_DAUTRE, jetons=1_000_000)
        with pytest.raises(HTTPException):
            await porte(owner="")

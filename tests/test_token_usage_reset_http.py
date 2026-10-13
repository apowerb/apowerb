"""Remise a zero du compteur de jetons, exercee a travers HTTP.

Le test existant appelle la fonction de route en contournant l'authentification
et la portee. Ici la requete traverse l'application FastAPI avec les vraies
dependances (``get_current_user`` decode un vrai JWT, ``require_admin``,
``_assert_may_act_on`` lit les vraies tables d'organisation) : une garde qui
ne tiendrait que parce qu'un test l'a neutralisee serait invisible autrement.

SQLite en memoire, schema des modeles attache s'il y en a un : les tables gardent leur
vrai schema, comme en PostgreSQL. Retirer le schema des tables ne suffit pas :
un test precedent (``test_default_superadmin``) peut avoir fige dans le cache
SQLAlchemy des colonnes de ``user`` qualifiees ``public``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from apowerb.admin import router as admin_router
from apowerb.configs.settings import get_settings
from apowerb.core import run_gate
from apowerb.core.agent_helpers.default_llm_usage import used_tokens_this_month
from apowerb.core.extensions.registry import registry
from apowerb.core.usage_quota import get_quota_status
from apowerb.helpers.database import Base, sessionmanager
from apowerb.helpers.security import create_access_token
from apowerb.models import LlmUsage, User, UserRole

SUPER = "super@exemple.fr"
ORG_ADMIN = "orgadmin@exemple.fr"
MEMBRE = "membre@exemple.fr"  # dans l'organisation de ORG_ADMIN
ETRANGER = "etranger@exemple.fr"  # hors de cette organisation
SIMPLE = "simple@exemple.fr"  # role USER


# Le schema des modeles, fige a l'import (`settings.db_schema`). Vide (la CI),
# les tables vivent dans `main`, le schema par defaut de SQLite.
SCHEMA = Base.metadata.schema or "main"


@pytest.fixture
async def factory(monkeypatch):
    # Une seule connexion : la base attachee vit avec elle.
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    # Le SQL brut des gardes et de la route cible `{db_schema}.table`.
    monkeypatch.setattr(get_settings(), "db_schema", SCHEMA)
    monkeypatch.setattr(admin_router, "_schema", lambda: SCHEMA)
    async with engine.begin() as conn:
        if SCHEMA != "main":
            await conn.exec_driver_sql(f"ATTACH DATABASE ':memory:' AS \"{SCHEMA}\"")
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text(f"CREATE TABLE {SCHEMA}.admin_organization (org_id INTEGER PRIMARY KEY)"))
        await conn.execute(
            text(f"CREATE TABLE {SCHEMA}.admin_org_member (user_id INTEGER PRIMARY KEY, org_id INTEGER NOT NULL)")
        )
        await conn.execute(text(f"CREATE TABLE {SCHEMA}.admin_superadmin (user_id INTEGER PRIMARY KEY)"))
        # Journal des remises a zero, ecrit dans la meme transaction que la coupure.
        await conn.execute(
            text(
                f"CREATE TABLE {SCHEMA}.admin_token_reset ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, "
                "target_email TEXT NOT NULL, admin_email TEXT NOT NULL, "
                "reset_at TIMESTAMP NOT NULL, tokens_before BIGINT NOT NULL)"
            )
        )

    sessions = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def _session() -> AsyncIterator[AsyncSession]:
        session = sessions()
        try:
            yield session
        finally:
            await session.close()

    monkeypatch.setattr(sessionmanager, "session", _session, raising=False)

    async with sessions() as db:
        for uid, mail, role in (
            (1, SUPER, UserRole.ADMIN),
            (2, ORG_ADMIN, UserRole.ADMIN),
            (3, MEMBRE, UserRole.USER),
            (4, ETRANGER, UserRole.USER),
            (5, SIMPLE, UserRole.USER),
        ):
            db.add(User(user_id=uid, email=mail, first_name="P", last_name="N", role=role))
        await db.flush()
        await db.execute(text(f"INSERT INTO {SCHEMA}.admin_organization (org_id) VALUES (1)"))
        await db.execute(text(f"INSERT INTO {SCHEMA}.admin_org_member (user_id, org_id) VALUES (2, 1), (3, 1)"))
        # Un superadmin nomme : sinon tout administrateur en serait un.
        await db.execute(text(f"INSERT INTO {SCHEMA}.admin_superadmin (user_id) VALUES (1)"))
        await db.commit()
    yield sessions
    await engine.dispose()


@pytest.fixture
async def client(factory):
    app = FastAPI()
    app.include_router(admin_router.router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _bearer(email: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token({'sub': email})}"}


async def _coupure(factory, user_id: int):
    async with factory() as db:
        return (
            await db.execute(select(User.token_usage_reset_at).where(User.user_id == user_id))
        ).scalar_one()


URL = "/admin/users/{}/reset-token-usage"


async def test_anonyme_recoit_401_et_rien_n_est_pose(client, factory):
    r = await client.post(URL.format(3))
    assert r.status_code == 401
    assert await _coupure(factory, 3) is None


async def test_jeton_invalide_recoit_401(client, factory):
    r = await client.post(URL.format(3), headers={"Authorization": "Bearer pas-un-jwt"})
    assert r.status_code == 401
    assert await _coupure(factory, 3) is None


async def test_utilisateur_non_admin_recoit_403_et_rien_n_est_pose(client, factory):
    r = await client.post(URL.format(3), headers=_bearer(SIMPLE))
    assert r.status_code == 403
    assert await _coupure(factory, 3) is None


async def test_non_admin_ne_peut_pas_se_remettre_a_zero_lui_meme(client, factory):
    r = await client.post(URL.format(5), headers=_bearer(SIMPLE))
    assert r.status_code == 403
    assert await _coupure(factory, 5) is None


async def test_admin_d_organisation_hors_perimetre_est_refuse_et_rien_n_est_pose(client, factory):
    """404 et non 403 : `_assert_may_act_on` cache volontairement l'existence
    d'un compte hors de la portee de l'administrateur."""
    r = await client.post(URL.format(4), headers=_bearer(ORG_ADMIN))
    assert r.status_code == 404
    assert await _coupure(factory, 4) is None


async def test_admin_d_organisation_dans_son_perimetre_pose_la_coupure(client, factory):
    avant = datetime.now(timezone.utc)
    r = await client.post(URL.format(3), headers=_bearer(ORG_ADMIN))
    assert r.status_code == 200
    assert r.json()["email"] == MEMBRE
    coupure = await _coupure(factory, 3)
    assert coupure is not None
    coupure = coupure if coupure.tzinfo else coupure.replace(tzinfo=timezone.utc)
    assert coupure >= avant - timedelta(seconds=1)


async def test_superadmin_atteint_un_compte_hors_organisation(client, factory):
    r = await client.post(URL.format(4), headers=_bearer(SUPER))
    assert r.status_code == 200
    assert await _coupure(factory, 4) is not None


async def test_compte_inconnu_recoit_404_pour_un_superadmin(client, factory):
    r = await client.post(URL.format(999), headers=_bearer(SUPER))
    assert r.status_code == 404


# ── Deux comptes : la coupure de A ne touche pas B ──────────────────────────


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


async def _compteurs(factory, owner: str) -> tuple[int, int]:
    async with factory() as db:
        garde = (await get_quota_status(db, owner_id=owner, plan=None)).used_tokens
        jauge = await used_tokens_this_month(db, owner)
    return garde, jauge


@pytest.fixture
def registre_vierge(monkeypatch):
    monkeypatch.setattr(registry, "_run_guards", [], raising=False)
    monkeypatch.setattr(registry, "_default_llm_cap", None, raising=False)
    return registry


async def test_remettre_a_zero_A_laisse_le_compteur_de_B_intact(client, factory):
    await _consomme(factory, MEMBRE, 300, 30)
    await _consomme(factory, ETRANGER, 700, 30)
    assert await _compteurs(factory, MEMBRE) == (300, 300)
    assert await _compteurs(factory, ETRANGER) == (700, 700)

    r = await client.post(URL.format(3), headers=_bearer(SUPER))
    assert r.status_code == 200

    assert await _compteurs(factory, MEMBRE) == (0, 0)
    assert await _compteurs(factory, ETRANGER) == (700, 700)
    assert await _coupure(factory, 4) is None


async def test_la_consommation_apres_la_coupure_de_A_recompte_pour_A_seulement(client, factory):
    await _consomme(factory, MEMBRE, 300, 30)
    await client.post(URL.format(3), headers=_bearer(SUPER))
    await _consomme(factory, MEMBRE, 50, 0)
    await _consomme(factory, ETRANGER, 10, 0)
    assert await _compteurs(factory, MEMBRE) == (50, 50)
    assert await _compteurs(factory, ETRANGER) == (10, 10)


async def test_plafond_glissant_A_repart_de_zero_B_reste_bloque(client, factory, registre_vierge, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "default_llm_user_token_cap", 1_000, raising=False)
    monkeypatch.setattr(s, "default_llm_global_token_cap", 0, raising=False)
    monkeypatch.setattr(s, "default_llm_cap_window_hours", 24, raising=False)

    await _consomme(factory, MEMBRE, 1_000, 30)
    await _consomme(factory, ETRANGER, 1_000, 30)
    for owner in (MEMBRE, ETRANGER):
        with pytest.raises(Exception) as bloque:
            await run_gate.apply_run_guards(agent_name="agent6", owner_id=owner, plan=None)
        assert getattr(bloque.value, "status_code", None) == 402

    r = await client.post(URL.format(3), headers=_bearer(SUPER))
    assert r.status_code == 200

    await run_gate.apply_run_guards(agent_name="agent6", owner_id=MEMBRE, plan=None)
    with pytest.raises(Exception) as toujours:
        await run_gate.apply_run_guards(agent_name="agent6", owner_id=ETRANGER, plan=None)
    assert getattr(toujours.value, "status_code", None) == 402

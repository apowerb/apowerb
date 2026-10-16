"""Remise a zero du compteur de jetons d'un compte, par un administrateur.

Rien n'est efface : ``llm_usage`` reste l'historique de consommation. La
remise a zero est une COUPURE posee sur le compte (``token_usage_reset_at``),
et chaque lecteur du compteur la respecte -- la garde mensuelle, la jauge
que voit l'utilisateur et le plafond glissant. Si un seul l'ignorait, la
jauge annoncerait un chiffre et la garde en appliquerait un autre.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from apowerb.admin import router as admin_router
from apowerb.configs.settings import get_settings
from apowerb.core import run_gate
from apowerb.core.agent_helpers import default_llm_usage as gauge_module
from apowerb.core.agent_helpers.default_llm_usage import (
    default_llm_usage,
    month_start,
    used_tokens_this_month,
)
from apowerb.core.extensions.registry import registry
from apowerb.core.usage_quota import get_quota_status
from apowerb.helpers.database import Base
from apowerb.models import LlmUsage, User

MOI = "eval@exemple.fr"
# Le schema des modeles (`settings.db_schema`), que la fixture `sqlite_engine`
# attache tel quel. Vide (la CI), les tables vivent dans `main`, le schema par
# defaut de SQLite. Aucun test ne le reecrit : la conftest l'interdit.
SCHEMA = Base.metadata.schema or "main"


@pytest.fixture
def registre_vierge(monkeypatch):
    monkeypatch.setattr(registry, "_run_guards", [], raising=False)
    monkeypatch.setattr(registry, "_default_llm_cap", None, raising=False)
    return registry


@pytest.fixture
async def base(sqlite_engine, bind_sessionmanager):
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    bind_sessionmanager(factory)
    async with factory() as db:
        db.add(User(user_id=7, email=MOI, first_name="E", last_name="G"))
        await db.commit()
    yield factory


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
async def test_la_route_admin_pose_la_coupure_sans_rien_effacer(base_admin, monkeypatch):
    base = base_admin
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


@pytest.fixture
async def base_admin(base, monkeypatch):
    """`base` + les tables du panneau d'admin que la route et la liste lisent.

    Le SQL brut de la route vise `{_schema()}.table` : le schema attache.
    """
    monkeypatch.setattr(admin_router, "_schema", lambda: SCHEMA)

    async def tout_voir(db, admin):
        return None

    monkeypatch.setattr(admin_router, "administered_user_ids", tout_voir)
    async with base() as db:
        for ddl in (
            f"CREATE TABLE {SCHEMA}.admin_superadmin (user_id INTEGER PRIMARY KEY, granted_at TIMESTAMP)",
            f"CREATE TABLE {SCHEMA}.admin_org_member (user_id INTEGER PRIMARY KEY, org_id INTEGER)",
            f"CREATE TABLE {SCHEMA}.admin_organization (org_id INTEGER PRIMARY KEY, name TEXT)",
            f"CREATE TABLE {SCHEMA}.admin_group_member (group_id INTEGER, user_id INTEGER)",
            f"CREATE TABLE {SCHEMA}.admin_group (group_id INTEGER PRIMARY KEY, name TEXT)",
            f"CREATE TABLE {SCHEMA}.th2agents_store (owner_id TEXT)",
            f"CREATE TABLE {SCHEMA}.admin_token_reset ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, "
            "target_email TEXT NOT NULL, admin_email TEXT NOT NULL, "
            "reset_at TIMESTAMP NOT NULL, tokens_before BIGINT NOT NULL)",
        ):
            await db.execute(text(ddl))
        await db.commit()
    return base


async def ajoute_compte(factory, user_id: int, email: str) -> None:
    async with factory() as db:
        db.add(User(user_id=user_id, email=email, first_name="A", last_name="B"))
        await db.commit()


@pytest.mark.asyncio
async def test_la_liste_admin_montre_ce_qui_compte_dans_le_quota(base_admin):
    base = base_admin
    await consomme(base, jetons=300, il_y_a_min=30)
    await remet_a_zero(base, il_y_a_min=10)
    await consomme(base, jetons=200, il_y_a_min=5)

    async with base() as db:
        users = await admin_router.list_users(db=db, _=MagicMock())
        garde, jauge = (
            (await get_quota_status(db, owner_id=MOI, plan=None)).used_tokens,
            await used_tokens_this_month(db, MOI),
        )
    moi = next(u for u in users if u.email == MOI)
    assert moi.tokens == 500, "la colonne historique garde tout"
    assert moi.quota_tokens == 200 == garde == jauge
    assert moi.token_usage_reset_at is not None


@pytest.mark.asyncio
async def test_la_liste_admin_sans_remise_a_zero_compte_le_mois_et_pas_avant(base_admin):
    base = base_admin
    await consomme(base, jetons=100, il_y_a_min=5)
    await consomme(base, jetons=999, il_y_a_min=60 * 24 * 40)

    async with base() as db:
        users = await admin_router.list_users(db=db, _=MagicMock())
    moi = users[0]
    assert (moi.quota_tokens, moi.tokens) == (100, 1099)
    assert moi.token_usage_reset_at is None


@pytest.mark.asyncio
async def test_la_liste_admin_ne_fait_pas_une_requete_par_compte(base_admin):
    base = base_admin
    for i in range(3):
        await ajoute_compte(base, 100 + i, f"autre{i}@exemple.fr")
    await consomme(base, jetons=100, il_y_a_min=5)

    vues: list[str] = []
    async with base() as db:
        bind = db.get_bind()
        event.listen(
            bind, "before_cursor_execute",
            lambda conn, cur, statement, *a: vues.append(statement),
        )
        await admin_router.list_users(db=db, _=MagicMock())
    sur_llm_usage = [q for q in vues if "llm_usage" in q]
    assert len(sur_llm_usage) == 2, sur_llm_usage


@pytest.mark.asyncio
async def test_la_route_renvoie_la_coupure_et_zero_jeton_compte(base_admin, monkeypatch):
    base = base_admin
    await consomme(base, jetons=300, il_y_a_min=30)
    admin = MagicMock(email="boss@exemple.fr")

    async def autorise(db, a, user_id):
        return None

    monkeypatch.setattr(admin_router, "_assert_may_act_on", autorise)
    async with base() as db:
        out = await admin_router.reset_token_usage(7, db=db, _=admin)
    assert out.token_usage_reset_at is not None
    assert out.quota_tokens == 0


@pytest.mark.asyncio
async def test_la_route_journalise_la_remise_a_zero(base_admin, monkeypatch):
    base = base_admin
    await consomme(base, jetons=300, il_y_a_min=30)
    admin = MagicMock(email="boss@exemple.fr")

    async def autorise(db, a, user_id):
        return None

    monkeypatch.setattr(admin_router, "_assert_may_act_on", autorise)
    async with base() as db:
        out = await admin_router.reset_token_usage(7, db=db, _=admin)

    async with base() as db:
        lignes = (await db.execute(text(
            "SELECT user_id, target_email, admin_email, tokens_before, reset_at "
            f"FROM {SCHEMA}.admin_token_reset"
        ))).all()
    assert len(lignes) == 1
    user_id, cible, qui, avant, quand = lignes[0]
    assert (user_id, cible, qui, avant) == (7, MOI, "boss@exemple.fr", 300)
    assert quand is not None
    assert out.token_usage_reset_at is not None


@pytest.mark.asyncio
async def test_un_compte_inconnu_ne_laisse_aucune_trace_d_audit(base_admin, monkeypatch):
    base = base_admin

    async def autorise(db, a, user_id):
        return None

    monkeypatch.setattr(admin_router, "_assert_may_act_on", autorise)
    async with base() as db:
        with pytest.raises(HTTPException):
            await admin_router.reset_token_usage(
                999, db=db, _=MagicMock(email="boss@exemple.fr")
            )
    async with base() as db:
        n = (await db.execute(text(f"SELECT count(*) FROM {SCHEMA}.admin_token_reset"))).scalar()
    assert n == 0


def test_la_table_d_audit_est_creee_par_la_migration_admin():
    from apowerb.admin import migration

    assert "admin_token_reset" in inspect.getsource(migration.ensure_admin_tables)


@pytest.fixture
def jauge_active(monkeypatch):
    monkeypatch.setattr(gauge_module, "default_llm_available", lambda: True)


@pytest.mark.asyncio
async def test_la_jauge_annonce_le_debut_effectif_apres_remise_a_zero(base, jauge_active):
    async with base() as db:
        user = (await db.execute(select(User).where(User.email == MOI))).scalar_one()
        coupure = datetime.now(timezone.utc) - timedelta(minutes=10)
        user.token_usage_reset_at = coupure
        await db.commit()
        vue = await default_llm_usage(db, owner_id=MOI, plan=None)
    debut = vue.period_start if vue.period_start.tzinfo else vue.period_start.replace(tzinfo=timezone.utc)
    assert abs(debut - coupure) < timedelta(seconds=1)


@pytest.mark.asyncio
async def test_la_jauge_garde_le_debut_du_mois_sans_remise_a_zero_utile(base, jauge_active):
    async with base() as db:
        assert (await default_llm_usage(db, owner_id=MOI, plan=None)).period_start == month_start()
    await remet_a_zero(base, il_y_a_min=60 * 24 * 40)
    async with base() as db:
        assert (await default_llm_usage(db, owner_id=MOI, plan=None)).period_start == month_start()

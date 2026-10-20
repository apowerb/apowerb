"""Jauge du modele mutualise « thaink2/default » : le coeur compte, une
brique plafonne (grille du 19/08/26 : voir est OSS, plafonner se vend).

Trois choses a proteger :

1. le mois est celui de Paris, la remise a zero aussi ;
2. la somme SQL ne compte que ce mois, ce proprietaire et ce qui a ete
   facture a thaink2 -- oublier un filtre ferait payer a un utilisateur la
   consommation d'un autre, celle de sa cle perso, ou celle du mois dernier ;
3. sans brique, aucune limite n'apparait ; avec une brique elle apparait, et
   une brique qui casse retombe sur le compteur seul, bruyamment.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from apowerb.core.agent_helpers import default_llm as dl
from apowerb.core.agent_helpers import default_llm_usage as du
from apowerb.core.extensions.registry import registry
from apowerb.helpers.database import Base
from apowerb.models import LlmUsage

UTC = timezone.utc
PARIS_SUMMER = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)  # Paris = UTC+2


# ------------------------------------------------------------- calendrier
def test_month_starts_at_paris_midnight():
    # 1er juillet 00:00 a Paris = 30 juin 22:00 UTC
    assert du.month_start(PARIS_SUMMER) == datetime(2026, 6, 30, 22, 0, tzinfo=UTC)


def test_reset_is_the_first_of_next_month_in_paris():
    assert du.next_reset(PARIS_SUMMER) == datetime(2026, 7, 31, 22, 0, tzinfo=UTC)


def test_december_rolls_over_to_january():
    december = datetime(2026, 12, 10, tzinfo=UTC)  # Paris = UTC+1
    assert du.next_reset(december) == datetime(2026, 12, 31, 23, 0, tzinfo=UTC)


def test_the_first_utc_hours_of_a_paris_month_already_count_in_it():
    """30 juin 23:30 UTC est deja le 1er juillet 01:30 a Paris : compter en
    UTC tirerait ces heures dans le mois precedent."""
    at = datetime(2026, 6, 30, 23, 30, tzinfo=UTC)
    assert du.month_start(at) == datetime(2026, 6, 30, 22, 0, tzinfo=UTC)


# ------------------------------------------------------------- assemblage
def test_without_a_cap_only_the_counter_is_served():
    usage = du.build_usage(1234, None, PARIS_SUMMER)
    assert usage.enabled and usage.used_tokens == 1234
    assert usage.limit_tokens is None
    assert usage.remaining_tokens is None
    assert usage.percent_used is None
    assert not usage.warning and not usage.exceeded
    assert usage.period_start == du.month_start(PARIS_SUMMER)
    assert usage.resets_at == du.next_reset(PARIS_SUMMER)


def test_a_zero_or_negative_cap_means_unlimited():
    assert du.build_usage(10, 0).limit_tokens is None
    assert du.build_usage(10, -5).limit_tokens is None


def test_with_a_cap_the_gauge_is_complete():
    usage = du.build_usage(250, 1000)
    assert (usage.limit_tokens, usage.remaining_tokens, usage.percent_used) == (
        1000,
        750,
        25.0,
    )
    assert not usage.warning and not usage.exceeded


def test_warning_from_80_percent_and_exceeded_at_the_cap():
    assert du.build_usage(800, 1000).warning
    assert not du.build_usage(799, 1000).warning
    over = du.build_usage(1500, 1000)
    assert over.exceeded
    assert over.remaining_tokens == 0
    assert over.percent_used == 100.0  # une barre ne deborde pas


def test_a_negative_counter_is_clamped_to_zero():
    assert du.build_usage(-3, None).used_tokens == 0


# ------------------------------------------------------------- somme SQL
@pytest.fixture
async def db() -> AsyncIterator:
    # Le MetaData du projet porte `schema=<DB_SCHEMA>` (helpers/database.py) :
    # SQLite refuse un schema inconnu, d'ou l'ATTACH ; StaticPool garde LA
    # connexion qui l'a recu.
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    schema = LlmUsage.__table__.schema
    async with engine.begin() as conn:
        if schema:
            await conn.execute(text(f"ATTACH DATABASE ':memory:' AS {schema}"))
        await conn.run_sync(Base.metadata.create_all, tables=[LlmUsage.__table__])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _add(db, **kw):
    row = dict(
        agent_id=1,
        agent_name="a",
        owner_id="dave@example.com",
        model="gemini/gemini-2.5-flash",
        billed_to_thaink2=True,
        total_tokens=100,
        created_at=datetime.now(UTC),
    )
    row.update(kw)
    db.add(LlmUsage(**row))
    await db.commit()


async def test_sums_this_month_for_this_owner(db):
    await _add(db, total_tokens=100)
    await _add(db, total_tokens=250)
    assert await du.used_tokens_this_month(db, "dave@example.com") == 350


async def test_no_rows_means_zero(db):
    assert await du.used_tokens_this_month(db, "dave@example.com") == 0


async def test_another_owner_is_excluded(db):
    await _add(db, total_tokens=100)
    await _add(db, owner_id="someone@else.com", total_tokens=9000)
    assert await du.used_tokens_this_month(db, "dave@example.com") == 100


async def test_personal_key_usage_is_excluded(db):
    """La consommation sur une cle perso est payee par l'utilisateur : la
    compter le penaliserait deux fois."""
    await _add(db, total_tokens=100, billed_to_thaink2=True)
    await _add(db, total_tokens=9000, billed_to_thaink2=False)
    assert await du.used_tokens_this_month(db, "dave@example.com") == 100


async def test_previous_month_is_excluded(db):
    last_month = du.month_start() - timedelta(minutes=1)
    await _add(db, total_tokens=100)
    await _add(db, total_tokens=9000, created_at=last_month)
    assert await du.used_tokens_this_month(db, "dave@example.com") == 100


async def test_the_window_is_closed_on_both_sides(db):
    """Une ligne datee dans le futur (horloge, import, restauration) ne doit
    pas gonfler le mois courant : la fenetre est [month_start, next_reset)."""
    await _add(db, total_tokens=100, created_at=du.month_start())  # borne incluse
    await _add(db, total_tokens=200, created_at=du.next_reset() - timedelta(minutes=1))
    await _add(db, total_tokens=9000, created_at=du.next_reset())  # borne exclue
    await _add(db, total_tokens=9000, created_at=du.next_reset() + timedelta(days=40))
    assert await du.used_tokens_this_month(db, "dave@example.com") == 300


# ------------------------------------------------------------- brique
class _FakeSettings:
    def __init__(self, model="", key=""):
        self.default_llm_model = model
        self.default_llm_api_key = key
        self.default_llm_api_base = ""


class _Db:
    """Juste de quoi rendre un scalaire : la requete est prouvee plus haut."""

    def __init__(self, total):
        self.total = total

    async def execute(self, _stmt):
        total = self.total

        class _Result:
            def scalar(self):
                return total

        return _Result()


@pytest.fixture
def registre_vierge(monkeypatch):
    monkeypatch.setattr(registry, "_default_llm_cap", None, raising=False)
    return registry


@pytest.fixture
def available(monkeypatch):
    monkeypatch.setattr(
        dl,
        "get_settings",
        lambda: _FakeSettings("gemini/gemini-2.5-flash", "sk-thaink2"),
    )


async def test_a_server_without_default_model_serves_nothing(
    registre_vierge, monkeypatch
):
    monkeypatch.setattr(dl, "get_settings", lambda: _FakeSettings())
    usage = await du.default_llm_usage(_Db(999), owner_id="a@b.fr", plan=None)
    assert usage == du.DefaultLlmUsage(enabled=False)


async def test_without_a_brick_there_is_no_limit(registre_vierge, available):
    usage = await du.default_llm_usage(_Db(120), owner_id="a@b.fr", plan="free")
    assert usage.enabled and usage.used_tokens == 120
    assert usage.limit_tokens is None


async def test_the_brick_provides_the_cap(registre_vierge, available):
    seen: list[tuple] = []

    async def cap(db, *, owner_id, plan):
        seen.append((owner_id, plan))
        return 1000

    registre_vierge.register_default_llm_cap(cap)
    usage = await du.default_llm_usage(_Db(800), owner_id="a@b.fr", plan="pro")
    assert seen == [("a@b.fr", "pro")]
    assert usage.limit_tokens == 1000
    assert usage.warning and not usage.exceeded


async def test_a_brick_may_answer_unlimited(registre_vierge, available):
    async def cap(db, *, owner_id, plan):
        return None

    registre_vierge.register_default_llm_cap(cap)
    usage = await du.default_llm_usage(_Db(5), owner_id="a@b.fr", plan="enterprise")
    assert usage.used_tokens == 5 and usage.limit_tokens is None


async def test_a_broken_brick_falls_back_to_the_counter_loudly(
    registre_vierge, available, caplog
):
    async def cap(db, *, owner_id, plan):
        raise RuntimeError("boom")

    registre_vierge.register_default_llm_cap(cap)
    with caplog.at_level("WARNING"):
        usage = await du.default_llm_usage(_Db(5), owner_id="a@b.fr", plan=None)
    assert usage.used_tokens == 5 and usage.limit_tokens is None
    assert "plafond illisible" in caplog.text


# ------------------------------------------------------------- route
def test_the_response_carries_neither_key_nor_model():
    """Le schema de reponse est ferme : personne ne pourra y glisser la cle
    mutualisee ou le modele sous-jacent sans casser ce test."""
    fields = set(du.DefaultLlmUsage.model_fields)
    assert fields == {
        "enabled",
        "used_tokens",
        "period_start",
        "resets_at",
        "limit_tokens",
        "remaining_tokens",
        "percent_used",
        "warning",
        "exceeded",
    }
    assert not any("key" in f or "model" in f or "base" in f for f in fields)


def test_the_schema_refuses_unknown_fields():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        du.DefaultLlmUsage(enabled=True, model_api_key="sk-thaink2")
    with pytest.raises(ValidationError):
        du.DefaultLlmUsage(enabled=True, model="gemini/gemini-2.5-flash")


@pytest.fixture
def mini_app(monkeypatch, registre_vierge, available):
    """L'application reduite au routeur config, avec une base factice : la
    dependance d'authentification, elle, est la vraie."""
    from fastapi import FastAPI

    from apowerb.auth import dependencies as auth_deps
    from apowerb.helpers.database import get_db
    from apowerb.routers.config import router

    monkeypatch.setattr(auth_deps, "BYPASS_AUTH", False, raising=False)
    app = FastAPI()
    app.include_router(router, prefix="/api")

    async def fake_db():
        yield _Db(4321)

    app.dependency_overrides[get_db] = fake_db
    return app


async def test_an_anonymous_request_is_refused(mini_app):
    import httpx

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mini_app), base_url="http://t"
    ) as client:
        response = await client.get("/api/config/default-llm/usage")
    assert response.status_code in (401, 403)
    assert "used_tokens" not in response.text


async def test_an_authenticated_request_gets_its_own_counter_and_nothing_secret(
    mini_app,
):
    import httpx

    from apowerb.auth.dependencies import get_current_user

    class _User:
        email = "dave@example.com"
        plan = "free"

    mini_app.dependency_overrides[get_current_user] = lambda: _User()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mini_app), base_url="http://t"
    ) as client:
        response = await client.get("/api/config/default-llm/usage")
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True and body["used_tokens"] == 4321
    assert body["limit_tokens"] is None and body["percent_used"] is None
    assert set(body) == set(du.DefaultLlmUsage.model_fields)
    assert "sk-thaink2" not in response.text
    assert "gemini" not in response.text


def test_the_route_is_mounted_and_authenticated():
    from apowerb.auth.dependencies import get_current_user
    from apowerb.routers import config as cfg

    route = next(r for r in cfg.router.routes if r.path == "/config/default-llm/usage")
    assert get_current_user in [d.call for d in route.dependant.dependencies]
    assert route.response_model is du.DefaultLlmUsage


async def test_cached_input_counts_at_a_quarter(db, monkeypatch):
    from apowerb.core import usage_quota as uq

    monkeypatch.delenv(uq.CACHED_TOKEN_PERCENT_ENV, raising=False)
    await _add(db, total_tokens=1000, cached_tokens=800)
    assert await du.used_tokens_this_month(db, "dave@example.com") == 400

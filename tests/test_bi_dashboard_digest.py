"""Resume hebdomadaire et alertes KPI d'un tableau de bord spec v2.

Aucun envoi reel : le canal mail (`send_system_email`), le calcul
(`compute_dashboard`) et le chargement du tableau de bord sont remplaces.
La base est une SQLite en memoire (memes tables que la production).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from apowerb.bi import dashboard_digest as dd
from apowerb.bi.spec.models import ComponentResult, DashboardDataResponse, KpiValue, VisualSpec
from apowerb.helpers.database import Base
from apowerb.models import BIDashboardDigest, Notification, User, UserRole
from tests.helpers.bi_spec_data import OWNER, build_dashboard, kpi

UTC = timezone.utc
NOW = datetime(2026, 10, 12, 8, 5, tzinfo=UTC)  # lundi


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    schema = BIDashboardDigest.__table__.schema
    async with engine.begin() as conn:
        if schema:
            await conn.execute(text(f"ATTACH DATABASE ':memory:' AS {schema}"))
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        user = User(first_name="Own", last_name="Er", email=OWNER, role=UserRole.USER)
        session.add(user)
        await session.commit()
        await session.refresh(user)
        session.owner_id = user.user_id  # type: ignore[attr-defined]
        yield session
    await engine.dispose()


def _dashboard():
    return build_dashboard(
        {
            "k_taux": kpi("taux").model_copy(update={"title": "Taux de service"}),
            "k_ca": kpi("ca").model_copy(update={"title": "Chiffre d'affaires"}),
            "t_sum": VisualSpec(type="text", source="ventes", text="Le service est stable."),
        },
        filters=[],
    )


def _response(taux="good", ca="good", taux_fmt="97 %"):
    return DashboardDataResponse(
        dashboard_id="dash-1",
        filters={},
        filter_options={},
        sources={},
        components={
            "k_taux": ComponentResult(type="kpi", value=KpiValue(value=0.97, formatted=taux_fmt, status=taux)),
            "k_ca": ComponentResult(type="kpi", value=KpiValue(value=10.0, formatted="10 k", status=ca)),
            "t_sum": ComponentResult(type="text", text="Le service est stable (97 %)."),
        },
    )


class Env:
    """Remplace charge/calcul/mail et enregistre les appels."""

    def __init__(self, monkeypatch):
        self.responses: list[DashboardDataResponse] = []
        self.compute_owners: list[str] = []
        self.load_owners: list[str] = []
        self.mails: list[dict] = []
        self.pushed: list[tuple[int, dict]] = []
        self.dashboard = _dashboard()

        async def load(db, dashboard_id, owner):
            self.load_owners.append(owner)
            return self.dashboard

        async def compute(dashboard, request, *, owner):
            self.compute_owners.append(owner)
            return self.responses.pop(0)

        async def mail(*, to, subject, html, text=None):
            self.mails.append({"to": to, "subject": subject, "html": html, "text": text})
            return True

        async def push(user_id, payload):
            self.pushed.append((user_id, payload))

        monkeypatch.setattr(dd, "load_dashboard", load)
        monkeypatch.setattr(dd, "compute_dashboard", compute)
        monkeypatch.setattr(dd, "send_system_email", mail)
        monkeypatch.setattr(dd, "push_notification", push)

    def alert_mails(self):
        return [m for m in self.mails if m["subject"].startswith("Alerte")]

    def digest_mails(self):
        return [m for m in self.mails if m["subject"].startswith("Résumé")]


@pytest.fixture
def env(monkeypatch):
    return Env(monkeypatch)


async def _subscribe(db, **kw):
    return await dd.DigestStore(db).upsert(dashboard_id="dash-1", owner_email=OWNER, now=NOW, **kw)


# --------------------------------------------------------------------------- rendu


def test_extract_kpis_and_texts_in_dashboard_order():
    kpis, texts = dd.extract(_dashboard(), _response(taux="warn"))
    assert [(k.id, k.label, k.formatted, k.status) for k in kpis] == [
        ("k_taux", "Taux de service", "97 %", "warn"),
        ("k_ca", "Chiffre d'affaires", "10 k", "good"),
    ]
    assert texts == ["Le service est stable (97 %)."]


def test_render_digest_contains_values_status_text_and_link():
    dash = _dashboard()
    kpis, texts = dd.extract(dash, _response(taux="bad"))
    subject, html, plain = dd.render_digest(dash, kpis, texts, link="https://app.example/bi/dash-1")
    assert subject == "Résumé hebdomadaire : Test spec"
    for needle in ("Taux de service", "97 %", "Chiffre d&#x27;affaires", "Le service est stable (97 %).", "https://app.example/bi/dash-1"):
        assert needle in html
    assert "Critique" in html and "OK" in html
    assert "Taux de service : 97 % (Critique)" in plain


def test_render_digest_escapes_html():
    dash = _dashboard()
    kpis, _ = dd.extract(dash, _response(taux_fmt="<script>x</script>"))
    _, html, _ = dd.render_digest(dash, kpis, [], link="/bi/dash-1")
    assert "<script>" not in html


# --------------------------------------------------------------------------- planification


def test_next_run_weekly_uses_timezone_and_is_strictly_after():
    # lundi 8 h Paris = 06:00 UTC en octobre (UTC+2)
    nxt = dd.next_run("weekly", weekday=0, hour=8, tz="Europe/Paris", after=NOW)
    assert nxt == datetime(2026, 10, 19, 6, 0, tzinfo=UTC)
    before = datetime(2026, 10, 12, 5, 0, tzinfo=UTC)
    assert dd.next_run("weekly", weekday=0, hour=8, tz="Europe/Paris", after=before) == datetime(2026, 10, 12, 6, 0, tzinfo=UTC)


def test_next_run_daily():
    nxt = dd.next_run("daily", weekday=None, hour=7, tz="UTC", after=NOW)
    assert nxt == datetime(2026, 10, 13, 7, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- alertes


async def test_alert_emitted_once_when_kpi_turns_bad(db, env):
    sub = await _subscribe(db)
    env.responses = [_response(taux="good"), _response(taux="bad"), _response(taux="bad")]
    await dd.process_subscription(db, sub, now=NOW, send_digest=False)
    assert env.alert_mails() == []
    await dd.process_subscription(db, sub, now=NOW, send_digest=False)
    assert len(env.alert_mails()) == 1
    assert "Taux de service" in env.alert_mails()[0]["html"]
    await dd.process_subscription(db, sub, now=NOW, send_digest=False)
    assert len(env.alert_mails()) == 1  # reste bad : pas de renvoi
    notifs = (await db.execute(select(Notification).where(Notification.type == "warning"))).scalars().all()
    assert len(notifs) == 1 and notifs[0].link == "/bi/dash-1" and notifs[0].user_id == db.owner_id
    assert len([p for p in env.pushed if p[1]["type"] == "warning"]) == 1


async def test_alert_rearmed_after_return_to_good_or_warn(db, env):
    sub = await _subscribe(db)
    env.responses = [_response(taux="bad"), _response(taux="warn"), _response(taux="bad")]
    for _ in range(3):
        await dd.process_subscription(db, sub, now=NOW, send_digest=False)
    assert len(env.alert_mails()) == 2


async def test_unknown_status_does_not_rearm(db, env):
    sub = await _subscribe(db)
    env.responses = [_response(taux="bad"), _response(taux=None), _response(taux="bad")]
    for _ in range(3):
        await dd.process_subscription(db, sub, now=NOW, send_digest=False)
    assert len(env.alert_mails()) == 1


async def test_alerts_disabled_sends_no_alert_but_digest_still_goes(db, env):
    sub = await _subscribe(db, alerts_enabled=False)
    env.responses = [_response(taux="bad")]
    await dd.process_subscription(db, sub, now=NOW, send_digest=True)
    assert env.alert_mails() == [] and len(env.digest_mails()) == 1


# --------------------------------------------------------------------------- resume


async def test_digest_sent_by_mail_and_in_app_with_owner_rights(db, env):
    sub = await _subscribe(db)
    env.responses = [_response(taux="good")]
    await dd.process_subscription(db, sub, now=NOW, send_digest=True)
    assert env.load_owners == [OWNER] and env.compute_owners == [OWNER]
    [mail] = env.digest_mails()
    assert mail["to"] == OWNER and "Taux de service" in mail["html"]
    info = (await db.execute(select(Notification).where(Notification.type == "info"))).scalars().all()
    assert len(info) == 1 and info[0].link == "/bi/dash-1"
    assert sub.last_sent_at is not None


async def test_missing_dashboard_does_not_raise_nor_send(db, env, monkeypatch):
    async def gone(db, dashboard_id, owner):
        return None

    monkeypatch.setattr(dd, "load_dashboard", gone)
    sub = await _subscribe(db)
    await dd.process_subscription(db, sub, now=NOW, send_digest=True)
    assert env.mails == []


# --------------------------------------------------------------------------- tick


async def test_tick_sends_due_digest_and_reschedules(db, env):
    sub = await _subscribe(db, frequency="weekly", weekday=0, hour=8, timezone="UTC")
    sub.next_digest_at = NOW - timedelta(minutes=1)
    sub.next_alert_check_at = NOW + timedelta(hours=1)
    await db.commit()
    env.responses = [_response()]
    assert await dd.tick_once(db, now=NOW) == 1
    assert len(env.digest_mails()) == 1
    await db.refresh(sub)
    assert sub.next_digest_at.replace(tzinfo=UTC) == datetime(2026, 10, 19, 8, 0, tzinfo=UTC)
    # deuxieme tick au meme instant : rien a refaire
    env.responses = [_response()]
    assert await dd.tick_once(db, now=NOW) == 0
    assert len(env.digest_mails()) == 1


async def test_tick_skips_when_nothing_is_due(db, env):
    await _subscribe(db)
    assert await dd.tick_once(db, now=NOW) == 0
    assert env.mails == []


async def test_tick_runs_alert_check_without_digest(db, env):
    sub = await _subscribe(db)
    sub.next_alert_check_at = NOW - timedelta(minutes=1)
    await db.commit()
    env.responses = [_response(taux="bad")]
    assert await dd.tick_once(db, now=NOW) == 1
    assert env.digest_mails() == [] and len(env.alert_mails()) == 1
    await db.refresh(sub)
    assert sub.next_alert_check_at.replace(tzinfo=UTC) == NOW + dd.ALERT_CHECK_INTERVAL


async def test_tick_survives_a_failing_subscription(db, env, monkeypatch):
    sub = await _subscribe(db)
    sub.next_digest_at = NOW - timedelta(minutes=1)
    await db.commit()

    async def boom(dashboard, request, *, owner):
        raise RuntimeError("calcul KO")

    monkeypatch.setattr(dd, "compute_dashboard", boom)
    assert await dd.tick_once(db, now=NOW) == 0
    await db.refresh(sub)
    assert sub.next_digest_at.replace(tzinfo=UTC) > NOW  # creneau consomme, pas de boucle infinie


@pytest.fixture
async def db_expiring():
    """Session de production : `expire_on_commit` garde son defaut (True)."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    schema = BIDashboardDigest.__table__.schema
    async with engine.begin() as conn:
        if schema:
            await conn.execute(text(f"ATTACH DATABASE ':memory:' AS {schema}"))
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine)() as session:
        session.add(User(first_name="Own", last_name="Er", email=OWNER, role=UserRole.USER))
        await session.commit()
        yield session
    await engine.dispose()


async def test_tick_handles_several_due_subscriptions_with_expiring_session(db_expiring, env):
    store = dd.DigestStore(db_expiring)
    for dash_id in ("dash-1", "dash-2", "dash-3"):
        sub = await store.upsert(dashboard_id=dash_id, owner_email=OWNER, now=NOW, timezone="UTC")
        sub.next_digest_at = NOW - timedelta(minutes=1)
    await db_expiring.commit()
    env.responses = [_response(), _response(), _response()]
    assert await dd.tick_once(db_expiring, now=NOW) == 3
    assert len(env.digest_mails()) == 3


# --------------------------------------------------------------------------- routes


@pytest.fixture
async def client(db):
    from apowerb.auth.dependencies import get_current_user
    from apowerb.helpers.database import get_db
    from apowerb.routers.bi_digest import router

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    current = {"email": OWNER}

    async def fake_db():
        yield db

    class _U:
        pass

    def fake_user():
        u = _U()
        u.email = current["email"]
        return u

    app.dependency_overrides[get_db] = fake_db
    app.dependency_overrides[get_current_user] = fake_user
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        c.current = current  # type: ignore[attr-defined]
        yield c


@pytest.fixture
async def saved_dashboard(db):
    from apowerb.bi.db_stores import DatabaseDashboardStore

    d = _dashboard()
    await DatabaseDashboardStore(db, owner=OWNER).save(d)
    return d


async def test_routes_crud(client, saved_dashboard, db):
    url = f"/api/v1/dashboards/{saved_dashboard.id}/digest"
    assert (await client.get(url)).status_code == 404

    body = {"frequency": "weekly", "weekday": 2, "hour": 9, "alerts_enabled": True, "timezone": "Europe/Paris"}
    r = await client.put(url, json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert (data["frequency"], data["weekday"], data["hour"], data["alerts_enabled"]) == ("weekly", 2, 9, True)
    assert data["dashboard_id"] == saved_dashboard.id and data["next_digest_at"]

    assert (await client.get(url)).json()["weekday"] == 2

    r = await client.put(url, json={**body, "frequency": "daily", "weekday": None, "alerts_enabled": False})
    assert r.status_code == 200 and r.json()["frequency"] == "daily" and r.json()["alerts_enabled"] is False
    rows = (await db.execute(select(BIDashboardDigest))).scalars().all()
    assert len(rows) == 1  # remplacement, pas de doublon

    assert (await client.delete(url)).status_code == 204
    assert (await client.get(url)).status_code == 404
    assert (await client.delete(url)).status_code == 404


async def test_routes_reject_invalid_input(client, saved_dashboard):
    url = f"/api/v1/dashboards/{saved_dashboard.id}/digest"
    for bad in (
        {"frequency": "monthly"},
        {"frequency": "weekly", "weekday": 9},
        {"frequency": "weekly", "hour": 24},
        {"frequency": "weekly", "timezone": "Mars/Olympus"},
    ):
        assert (await client.put(url, json=bad)).status_code == 422, bad


async def test_routes_are_owner_scoped(client, saved_dashboard):
    url = f"/api/v1/dashboards/{saved_dashboard.id}/digest"
    client.current["email"] = "intruder@example.com"
    assert (await client.put(url, json={"frequency": "weekly"})).status_code == 404
    assert (await client.get(url)).status_code == 404


async def test_routes_refuse_a_v1_dashboard(client, db):
    from apowerb.bi.dashboards.core import Dashboard
    from apowerb.bi.db_stores import DatabaseDashboardStore

    v1 = Dashboard(title="Ancien", created_by=OWNER)
    await DatabaseDashboardStore(db, owner=OWNER).save(v1)
    r = await client.put(f"/api/v1/dashboards/{v1.id}/digest", json={"frequency": "weekly"})
    assert r.status_code == 422

"""Trigger ``email`` (Outlook) sans agent : un abonnement Microsoft Graph
propre aux workflows.

Avant : ``dispatch_email_triggers`` n'était appelé que depuis le chemin
agent de ``process_webhook_log_row`` — un workflow n'entendait un mail que si
un agent de son propriétaire était abonné à la boîte (``agent_id`` NOT NULL),
et cet agent tournait aussi à chaque mail.

Ce fichier couvre :

* le schéma — ``agent_id`` nullable (abonnement et journal), un seul
  abonnement workflow par propriétaire et fournisseur ;
* la réconciliation — créer l'abonnement quand un trigger email Outlook est
  armé, le retirer quand plus aucun ne l'est, ne jamais toucher aux
  abonnements agents ;
* le traitement d'une notification sur cet abonnement — workflows lancés,
  aucun agent ;
* la non-régression du chemin agent existant — l'agent tourne toujours, et les
  workflows ne sont pas lancés deux fois quand les deux abonnements existent.

Non exercé : la réception réelle d'une notification Graph (abonnement live).
"""

from __future__ import annotations

import contextlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from apowerb.core import workflow_mail_subscriptions as wms
from apowerb.models import Integration, User, WebhookLog, WebhookSubscription

ALICE = "alice@acme.fr"
BOB = "bob@other.fr"


# --- Schéma -----------------------------------------------------------------


def test_agent_id_is_nullable_on_subscriptions_and_logs():
    assert WebhookSubscription.__table__.c.agent_id.nullable is True
    assert WebhookLog.__table__.c.agent_id.nullable is True


def test_consumer_names_who_handles_the_mail_explicitly():
    """Qui traite un mail se lit dans UNE colonne, ``consumer``, jamais en
    déduisant d'un ``agent_id`` vide. Valeurs connues : agent, workflow."""
    from apowerb.models import WEBHOOK_CONSUMERS

    assert WEBHOOK_CONSUMERS == ("agent", "workflow")
    for model in (WebhookSubscription, WebhookLog):
        col = model.__table__.c.consumer
        assert col.nullable is False
        # Les lignes existantes (toutes d'agents) deviennent "agent".
        assert "agent" in str(col.server_default.arg)


async def test_the_database_refuses_an_inconsistent_consumer(factory):
    """Un agent sans agent_id, un workflow avec un agent_id, ou un consumer
    inconnu : refusés par la base, pas seulement par le code."""
    bad = [
        dict(consumer="agent", agent_id=None),
        dict(consumer="workflow", agent_id=6),
        dict(consumer="robot", agent_id=None),
    ]
    for values in bad:
        async with factory() as db:
            db.add(
                WebhookSubscription(
                    user_id=1, provider="microsoft_outlook", resource="r",
                    notification_url="u", client_state="c", status="active", **values,
                )
            )
            with pytest.raises(IntegrityError):
                await db.commit()


def test_subscription_migration_drops_not_null_and_adds_the_unique_index():
    """Une base existante a ``agent_id NOT NULL`` : la migration doit le lever
    et poser l'index unique partiel, sans rien faire quand c'est déjà fait."""
    from unittest.mock import MagicMock, patch

    import apowerb.helpers.webhook_migration as mod

    def _run(nullable):
        conn = MagicMock()
        engine = MagicMock()
        engine.connect.return_value.__enter__.return_value = conn
        inspector = MagicMock()
        inspector.get_columns.return_value = [
            {"name": "last_history_id", "nullable": True},
            {"name": "agent_id", "nullable": nullable},
        ] + ([] if nullable is False else [{"name": "consumer", "nullable": False}])
        inspector.get_check_constraints.return_value = (
            [] if nullable is False else [{"name": "ck_webhook_subscriptions_consumer"}]
        )
        with patch.object(mod, "create_engine", return_value=engine), patch.object(
            mod, "inspect", return_value=inspector
        ), patch.object(mod, "DBConfig") as cfg:
            cfg.return_value.get_db_url.return_value = "postgresql+asyncpg://x/y"
            mod.ensure_webhook_subscriptions_columns()
        return [str(c.args[0]) for c in conn.execute.call_args_list]

    sql = " ".join(_run(nullable=False))
    assert "ALTER COLUMN agent_id DROP NOT NULL" in sql
    assert "ADD COLUMN IF NOT EXISTS consumer VARCHAR(20) NOT NULL DEFAULT 'agent'" in sql
    assert "ADD CONSTRAINT ck_webhook_subscriptions_consumer" in sql
    assert "CREATE UNIQUE INDEX IF NOT EXISTS ux_webhook_subscriptions_workflow_owner" in sql
    assert "WHERE consumer = 'workflow'" in sql

    sql = " ".join(_run(nullable=True))
    assert "DROP NOT NULL" not in sql
    assert "ADD COLUMN" not in sql
    assert "ADD CONSTRAINT" not in sql
    # L'index reste idempotent (IF NOT EXISTS) : le reposer ne coûte rien.
    assert "ux_webhook_subscriptions_workflow_owner" in sql


def test_log_migration_drops_not_null_on_agent_id():
    from unittest.mock import MagicMock, patch

    import apowerb.helpers.webhook_log_migration as mod

    conn = MagicMock()
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value = conn
    inspector = MagicMock()
    inspector.get_table_names.return_value = ["webhook_logs"]
    present = [n for n, _ in mod._COLONNES_AJOUTEES]
    inspector.get_columns.return_value = [
        {"name": n, "nullable": True} for n in present if n != "consumer"
    ] + [{"name": "agent_id", "nullable": False}]
    inspector.get_check_constraints.return_value = []
    with patch.object(mod, "create_engine", return_value=engine), patch.object(
        mod, "inspect", return_value=inspector
    ), patch.object(mod, "DBConfig") as cfg:
        cfg.return_value.get_db_url.return_value = "postgresql+asyncpg://x/y"
        mod.ensure_webhook_logs_columns()
    sql = " ".join(str(c.args[0]) for c in conn.execute.call_args_list)
    assert "ALTER COLUMN agent_id DROP NOT NULL" in sql
    assert "ADD COLUMN IF NOT EXISTS consumer VARCHAR(20) NOT NULL DEFAULT 'agent'" in sql
    assert "ADD CONSTRAINT ck_webhook_logs_consumer" in sql


# --- Base SQLite + faux Graph ----------------------------------------------


@pytest.fixture
async def factory(sqlite_engine, bind_sessionmanager):
    f = async_sessionmaker(sqlite_engine, expire_on_commit=True)
    bind_sessionmanager(f)
    async with f() as db:
        for uid, email in ((1, ALICE), (2, BOB)):
            db.add(
                User(
                    user_id=uid, first_name="T", last_name="T", email=email,
                    password="x", role="USER",
                )
            )
        await db.commit()
        db.add(Integration(user_id=1, provider="microsoft_outlook"))
        db.add(Integration(user_id=2, provider="microsoft_outlook"))
        await db.commit()
    return f


@pytest.fixture
def graph(monkeypatch):
    """Faux Microsoft Graph : chaque création renvoie un id distinct."""
    calls = {"create": [], "delete": []}

    async def _create(**kw):
        calls["create"].append(kw)
        return {
            "id": f"graph-{len(calls['create'])}",
            "expirationDateTime": "2026-10-22T10:00:00Z",
        }

    async def _delete(token, sub_id):
        calls["delete"].append(sub_id)
        return True

    svc = wms.OutlookWebhookService
    monkeypatch.setattr(svc, "get_access_token_for_user", AsyncMock(return_value="tok"))
    monkeypatch.setattr(svc, "create_subscription", AsyncMock(side_effect=_create))
    monkeypatch.setattr(svc, "delete_subscription", AsyncMock(side_effect=_delete))
    monkeypatch.setattr(svc, "generate_client_state", lambda: "state-wf")
    monkeypatch.setattr(wms, "_notification_url", lambda: "https://api.example/api/webhooks/outlook/notifications")
    return calls


def _armed(monkeypatch, *owners_providers):
    """Triggers email actifs, sous la forme renvoyée par ``list_active_triggers``."""
    rows = [
        {"workflow_id": f"wf{i}", "owner_id": owner, "config": json.dumps({"kind": "email", "provider": prov})}
        for i, (owner, prov) in enumerate(owners_providers)
    ]
    monkeypatch.setattr(
        wms.wt, "list_active_triggers",
        lambda kind, owner_id=None: rows if kind == "email" else [],
    )


async def _subs(factory):
    async with factory() as db:
        rows = (await db.execute(select(WebhookSubscription).order_by(WebhookSubscription.id))).scalars().all()
        return [
            SimpleNamespace(
                id=r.id, user_id=r.user_id, agent_id=r.agent_id, consumer=r.consumer,
                status=r.status,
                subscription_id=r.subscription_id, client_state=r.client_state,
                resource=r.resource, expiration_datetime=r.expiration_datetime,
                updated_at=r.updated_at,
            )
            for r in rows
        ]


async def _add_agent_sub(factory, user_id=1, graph_id="agent-graph", agent_id=6):
    async with factory() as db:
        db.add(
            WebhookSubscription(
                user_id=user_id, provider="microsoft_outlook", subscription_id=graph_id,
                resource="me/mailFolders('Inbox')/messages", change_type="created",
                notification_url="https://x/cb", client_state="agent-state",
                agent_id=agent_id, status="active",
            )
        )
        await db.commit()


# --- Réconciliation ----------------------------------------------------------


async def test_an_armed_outlook_trigger_gets_its_own_subscription(factory, graph, monkeypatch):
    _armed(monkeypatch, (ALICE, "outlook"))

    await wms.reconcile_outlook_subscriptions()

    subs = await _subs(factory)
    assert len(subs) == 1
    s = subs[0]
    assert (s.user_id, s.consumer, s.agent_id, s.status, s.subscription_id) == (
        1, "workflow", None, "active", "graph-1",
    )
    assert s.client_state == "state-wf"
    assert s.expiration_datetime is not None
    created = graph["create"][0]
    assert created["notification_url"].endswith("/api/webhooks/outlook/notifications")
    assert created["resource"] == "me/mailFolders('Inbox')/messages"
    assert created["client_state"] == "state-wf"


async def test_reconcile_is_idempotent_and_shares_one_subscription_per_owner(
    factory, graph, monkeypatch
):
    """Deux workflows du même propriétaire : UN abonnement — deux abonnements
    sur la même boîte livreraient deux notifications, donc deux runs par
    workflow."""
    _armed(monkeypatch, (ALICE, "outlook"), (ALICE, "outlook"))

    await wms.reconcile_outlook_subscriptions()
    await wms.reconcile_outlook_subscriptions()

    assert len(await _subs(factory)) == 1
    assert len(graph["create"]) == 1


async def test_gmail_triggers_do_not_create_an_outlook_subscription(factory, graph, monkeypatch):
    _armed(monkeypatch, (ALICE, "gmail"))
    await wms.reconcile_outlook_subscriptions()
    assert await _subs(factory) == []
    assert graph["create"] == []


async def test_no_integration_no_subscription(factory, graph, monkeypatch):
    _armed(monkeypatch, ("carol@nowhere.fr", "outlook"))
    await wms.reconcile_outlook_subscriptions()
    assert await _subs(factory) == []
    assert graph["create"] == []


async def test_disarming_the_last_trigger_removes_the_workflow_subscription_only(
    factory, graph, monkeypatch
):
    await _add_agent_sub(factory, user_id=1)
    _armed(monkeypatch, (ALICE, "outlook"))
    await wms.reconcile_outlook_subscriptions()
    assert len(await _subs(factory)) == 2

    _armed(monkeypatch)  # plus aucun trigger armé
    await wms.reconcile_outlook_subscriptions()

    subs = await _subs(factory)
    assert [(s.agent_id, s.subscription_id) for s in subs] == [(6, "agent-graph")]
    assert graph["delete"] == ["graph-1"]


async def test_agent_subscriptions_are_never_touched(factory, graph, monkeypatch):
    """Un abonnement agent en production ne doit être ni supprimé, ni réutilisé, ni
    modifié par la réconciliation, trigger armé ou non."""
    await _add_agent_sub(factory, user_id=2, graph_id="prod-agent-graph", agent_id=42)
    _armed(monkeypatch)
    await wms.reconcile_outlook_subscriptions()
    _armed(monkeypatch, (BOB, "outlook"))
    await wms.reconcile_outlook_subscriptions()

    subs = await _subs(factory)
    agent = [s for s in subs if s.agent_id == 42]
    assert len(agent) == 1
    assert (agent[0].subscription_id, agent[0].status, agent[0].client_state) == (
        "prod-agent-graph", "active", "agent-state",
    )
    assert "prod-agent-graph" not in graph["delete"]


async def test_a_graph_failure_is_recorded_and_retried_after_the_backoff(
    factory, graph, monkeypatch
):
    _armed(monkeypatch, (ALICE, "outlook"))
    wms.OutlookWebhookService.create_subscription.side_effect = RuntimeError("HTTP 400")

    await wms.reconcile_outlook_subscriptions()
    subs = await _subs(factory)
    assert [(s.agent_id, s.status, s.subscription_id) for s in subs] == [(None, "error", None)]

    # Dans la fenêtre de backoff : pas de nouvel appel à Graph.
    await wms.reconcile_outlook_subscriptions()
    assert wms.OutlookWebhookService.create_subscription.await_count == 1

    # Après la fenêtre : nouvel essai, qui réussit.
    wms.OutlookWebhookService.create_subscription.side_effect = None
    wms.OutlookWebhookService.create_subscription.return_value = {
        "id": "graph-ok", "expirationDateTime": "2026-10-22T10:00:00Z",
    }
    later = datetime.now(timezone.utc) + wms.RETRY_AFTER + timedelta(seconds=5)
    await wms.reconcile_outlook_subscriptions(now=later)
    subs = await _subs(factory)
    assert [(s.status, s.subscription_id) for s in subs] == [("active", "graph-ok")]


async def test_an_expired_workflow_subscription_is_recreated(factory, graph, monkeypatch):
    """La boucle de renouvellement marque ``expired`` un abonnement qu'elle n'a
    pas pu prolonger : la réconciliation doit en recréer un."""
    _armed(monkeypatch, (ALICE, "outlook"))
    await wms.reconcile_outlook_subscriptions()
    async with factory() as db:
        row = (await db.execute(select(WebhookSubscription))).scalar_one()
        row.status = "expired"
        await db.commit()

    later = datetime.now(timezone.utc) + wms.RETRY_AFTER + timedelta(seconds=5)
    await wms.reconcile_outlook_subscriptions(now=later)

    subs = await _subs(factory)
    assert [(s.status, s.subscription_id) for s in subs] == [("active", "graph-2")]
    assert graph["delete"] == ["graph-1"]  # l'ancien est retiré côté Graph


async def test_only_one_workflow_subscription_per_owner_in_the_database(factory):
    """Garde multi-workers : deux processus qui réconcilient en même temps ne
    peuvent pas créer deux lignes pour le même propriétaire."""
    async with factory() as db:
        for _ in range(2):
            db.add(
                WebhookSubscription(
                    user_id=1, provider="microsoft_outlook", resource="r",
                    notification_url="u", client_state="c", agent_id=None,
                    consumer="workflow", status="pending",
                )
            )
        with pytest.raises(IntegrityError):
            await db.commit()


async def test_has_active_workflow_subscription(factory, graph, monkeypatch):
    async with factory() as db:
        assert await wms.has_active_workflow_subscription(db, 1) is False
    await _add_agent_sub(factory, user_id=1)
    async with factory() as db:
        assert await wms.has_active_workflow_subscription(db, 1) is False
    _armed(monkeypatch, (ALICE, "outlook"))
    await wms.reconcile_outlook_subscriptions()
    async with factory() as db:
        assert await wms.has_active_workflow_subscription(db, 1) is True
        assert await wms.has_active_workflow_subscription(db, 2) is False


# --- Réception de la notification -------------------------------------------


async def test_a_notification_on_the_workflow_subscription_is_enqueued_without_agent(
    factory, graph, monkeypatch
):
    from fastapi import BackgroundTasks

    import apowerb.routers.webhook_handlers.outlook as ol

    _armed(monkeypatch, (ALICE, "outlook"))
    await wms.reconcile_outlook_subscriptions()

    class _Req:
        async def json(self):
            return {
                "value": [
                    {
                        "subscriptionId": "graph-1",
                        "clientState": "state-wf",
                        "changeType": "created",
                        "resource": "Users/u/Messages/m1",
                    }
                ]
            }

    resp = await ol.handle_outlook_notification(_Req(), BackgroundTasks(), None)
    assert resp.status_code == 202
    async with factory() as db:
        log = (await db.execute(select(WebhookLog))).scalar_one()
        assert (log.consumer, log.agent_id, log.user_id, log.resource_id, log.status) == (
            "workflow", None, 1, "Users/u/Messages/m1", "pending",
        )


# --- Traitement par le worker -------------------------------------------------


def _email(subject="AR commande 42"):
    return {
        "subject": subject,
        "from": {"emailAddress": {"address": "fournisseur@acme.fr"}},
        "toRecipients": [{"emailAddress": {"address": ALICE}}],
        "body": {"contentType": "text", "content": "Bonjour"},
        "receivedDateTime": "2026-10-19T08:00:00Z",
        "hasAttachments": False,
    }


def _fake_db(log_row, sub_row, owner, *, workflow_sub_active):
    class _DB:
        async def get(self, model, _id):
            if model is WebhookLog:
                return log_row
            if model is WebhookSubscription:
                return sub_row
            if model is User:
                return owner
            return None

        async def commit(self):
            return None

    @contextlib.asynccontextmanager
    async def _session():
        yield _DB()

    return _session


def _wire(monkeypatch, ol, *, log_row, workflow_sub_active, fetch=None):
    owner = SimpleNamespace(user_id=1, email=ALICE)
    sub_row = SimpleNamespace(agent_message_template=None)
    monkeypatch.setattr(
        ol.sessionmanager, "session",
        _fake_db(log_row, sub_row, owner, workflow_sub_active=workflow_sub_active),
    )
    monkeypatch.setattr(
        ol.OutlookWebhookService, "get_access_token_for_user", AsyncMock(return_value="tok")
    )
    monkeypatch.setattr(
        ol.OutlookWebhookService, "fetch_email", fetch or AsyncMock(return_value=_email())
    )
    monkeypatch.setattr(
        ol.workflow_mail_subscriptions, "has_active_workflow_subscription",
        AsyncMock(return_value=workflow_sub_active),
    )
    dispatched = AsyncMock(return_value=["run-1"])
    monkeypatch.setattr(ol.workflow_email_triggers, "dispatch_email_triggers", dispatched)
    ran = AsyncMock(return_value="agent done")
    monkeypatch.setattr(ol, "run_agent_for_webhook", ran)
    monkeypatch.setattr(ol, "create_webhook_notification", AsyncMock(), raising=False)
    monkeypatch.setattr(ol, "_emit_run_outcome", AsyncMock())
    monkeypatch.setattr(ol, "_augment_agent_response", AsyncMock(side_effect=lambda **kw: kw["response"]))
    return dispatched, ran


def _log(agent_id, consumer=None):
    return SimpleNamespace(
        id=7, subscription_id=1, user_id=1, agent_id=agent_id,
        consumer=consumer or ("workflow" if agent_id is None else "agent"), resource_id="Users/u/Messages/m1",
        created_at=None, force_reprocess=False, agent_message=None, email_subject=None,
        email_sender=None, email_body_html=None, email_body_text=None, attachments=None,
        trigger_event="created",
    )


async def test_worker_runs_the_workflows_and_no_agent_for_a_workflow_log(monkeypatch):
    import apowerb.routers.webhook_handlers.outlook as ol

    log_row = _log(agent_id=None)
    dispatched, ran = _wire(monkeypatch, ol, log_row=log_row, workflow_sub_active=True)

    out = await ol.process_webhook_log_row(7)

    ran.assert_not_awaited()
    dispatched.assert_awaited_once()
    kw = dispatched.await_args.kwargs
    assert kw["provider"] == "outlook" and kw["owner_id"] == ALICE
    env = kw["envelope"]
    assert (env["from"], env["to"], env["subject"], env["body"]) == (
        "fournisseur@acme.fr", ALICE, "AR commande 42", "Bonjour",
    )
    assert env["received_at"] == "2026-10-19T08:00:00Z"
    assert "run-1" in out
    # Observabilité : la ligne du journal porte le mail, comme pour un agent.
    assert log_row.email_subject == "AR commande 42"
    assert log_row.email_sender == "fournisseur@acme.fr"


async def test_a_workflow_log_whose_mail_left_the_mailbox_is_skipped(monkeypatch):
    """Pas de copie à rejouer pour un workflow (rien n'a encore été capturé) :
    le mail parti, il n'y a rien à lancer — ni exception (pas de reprise
    infinie), ni run sur un mail vide."""
    import apowerb.routers.webhook_handlers.outlook as ol

    gone = AsyncMock(side_effect=RuntimeError("HTTP 404 ErrorItemNotFound"))
    dispatched, ran = _wire(
        monkeypatch, ol, log_row=_log(agent_id=None), workflow_sub_active=True, fetch=gone
    )

    out = await ol.process_webhook_log_row(7)

    dispatched.assert_not_awaited()
    ran.assert_not_awaited()
    assert "SKIP" in out


async def test_a_workflow_log_propagates_graph_errors_other_than_404(monkeypatch):
    import apowerb.routers.webhook_handlers.outlook as ol

    boom = AsyncMock(side_effect=RuntimeError("HTTP 401 InvalidAuthenticationToken"))
    _wire(monkeypatch, ol, log_row=_log(agent_id=None), workflow_sub_active=True, fetch=boom)
    with pytest.raises(RuntimeError, match="401"):
        await ol.process_webhook_log_row(7)


# --- Non-régression : chemin agent -------------------------------------------


async def test_agent_log_still_runs_the_agent_and_dispatches_without_workflow_sub(monkeypatch):
    """Sans abonnement workflow, le chemin agent reste le seul à entendre le
    mail : il lance l'agent ET les workflows, comme avant."""
    import apowerb.routers.webhook_handlers.outlook as ol

    dispatched, ran = _wire(monkeypatch, ol, log_row=_log(agent_id=6), workflow_sub_active=False)

    await ol.process_webhook_log_row(7)

    ran.assert_awaited_once()
    assert ran.await_args.kwargs["agent_id"] == 6
    dispatched.assert_awaited_once()


async def test_agent_log_does_not_dispatch_when_the_workflow_sub_exists(monkeypatch):
    """Les deux abonnements livrent chacun une notification pour le même
    mail : seul l'abonnement workflow lance les workflows, sinon ils
    partiraient deux fois. L'agent, lui, tourne toujours."""
    import apowerb.routers.webhook_handlers.outlook as ol

    dispatched, ran = _wire(monkeypatch, ol, log_row=_log(agent_id=6), workflow_sub_active=True)

    await ol.process_webhook_log_row(7)

    ran.assert_awaited_once()
    dispatched.assert_not_awaited()


async def test_agent_log_survives_a_failing_workflow_sub_lookup(monkeypatch):
    import apowerb.routers.webhook_handlers.outlook as ol

    dispatched, ran = _wire(monkeypatch, ol, log_row=_log(agent_id=6), workflow_sub_active=False)
    monkeypatch.setattr(
        ol.workflow_mail_subscriptions, "has_active_workflow_subscription",
        AsyncMock(side_effect=RuntimeError("db down")),
    )
    await ol.process_webhook_log_row(7)
    ran.assert_awaited_once()


# --- API agents : liste des abonnements ---------------------------------------


async def test_subscription_list_hides_workflow_subscriptions(factory, graph, monkeypatch):
    """L'écran des abonnements est celui des agents (``agent_id`` requis par
    son schéma) : l'abonnement workflow, géré par le système, n'y figure pas."""
    from apowerb.routers import webhooks

    await _add_agent_sub(factory, user_id=1)
    _armed(monkeypatch, (ALICE, "outlook"))
    await wms.reconcile_outlook_subscriptions()

    async with factory() as db:
        out = await webhooks.list_subscriptions(db=db, current_user=SimpleNamespace(user_id=1))
    assert [(s.consumer, s.agent_id) for s in out.subscriptions] == [("agent", 6)]


# --- Statut du trigger --------------------------------------------------------


def test_trigger_status_reports_a_missing_subscription(monkeypatch):
    from apowerb.core import workflow_triggers as wt

    monkeypatch.setattr(wt, "integration_present", lambda owner, provider: True)
    cfg = {"kind": "email", "provider": "outlook"}

    monkeypatch.setattr(wt, "mail_subscription_active", lambda owner, provider: False)
    assert wt.subscription_reason(cfg, owner_id=ALICE) == "subscription_pending"

    monkeypatch.setattr(wt, "mail_subscription_active", lambda owner, provider: True)
    assert wt.subscription_reason(cfg, owner_id=ALICE) is None

    # Gmail : pas d'abonnement workflow en v1, statut inchangé.
    assert wt.subscription_reason({"kind": "email", "provider": "gmail"}, owner_id=ALICE) is None


# --- Branchement ---------------------------------------------------------------


async def test_the_scheduler_loop_reconciles_even_when_the_tick_fails(monkeypatch):
    import asyncio

    from apowerb.core import flow_scheduler

    monkeypatch.setattr(flow_scheduler, "tick_once", AsyncMock(side_effect=RuntimeError("tick")))
    reconcile = AsyncMock(return_value={})
    monkeypatch.setattr(
        flow_scheduler.workflow_mail_subscriptions, "reconcile_outlook_subscriptions", reconcile
    )
    monkeypatch.setattr(
        flow_scheduler.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError)
    )
    with pytest.raises(asyncio.CancelledError):
        await flow_scheduler._loop()
    reconcile.assert_awaited_once()


async def test_an_unknown_consumer_fails_loudly(monkeypatch):
    """Un consumer que le worker ne sait pas traiter : échec visible, jamais
    traité par défaut comme un agent (ni comme un workflow)."""
    import apowerb.routers.webhook_handlers.outlook as ol

    dispatched, ran = _wire(
        monkeypatch, ol, log_row=_log(agent_id=None, consumer="robot"), workflow_sub_active=False
    )
    with pytest.raises(RuntimeError, match="consumer"):
        await ol.process_webhook_log_row(7)
    ran.assert_not_awaited()
    dispatched.assert_not_awaited()


async def test_webhook_log_rows_expose_their_consumer(factory, graph, monkeypatch):
    """L'onglet Activity doit dire qui a traité chaque mail."""
    from apowerb.routers import webhooks

    _armed(monkeypatch, (ALICE, "outlook"))
    await wms.reconcile_outlook_subscriptions()
    async with factory() as db:
        sub_id = (await db.execute(select(WebhookSubscription.id))).scalar_one()
        db.add(
            WebhookLog(
                user_id=1, subscription_id=sub_id, agent_id=None, consumer="workflow",
                trigger_event="created", resource_id="m1", status="success",
            )
        )
        await db.commit()
        log_id = (await db.execute(select(WebhookLog.id))).scalar_one()
        out = await webhooks.get_webhook_log_by_id(
            log_id, db=db, current_user=SimpleNamespace(user_id=1, email=ALICE)
        )
    assert out["log"]["consumer"] == "workflow"


async def test_the_agent_patch_route_cannot_touch_the_workflow_subscription(
    factory, graph, monkeypatch
):
    """Géré par le système, invisible dans la liste : un PATCH dessus répond
    404 au lieu d'y poser un agent (que la contrainte CHECK refuserait)."""
    from fastapi import HTTPException

    from apowerb.routers import webhooks
    from apowerb.schema.webhook_schema import WebhookSubscriptionUpdate

    _armed(monkeypatch, (ALICE, "outlook"))
    await wms.reconcile_outlook_subscriptions()
    async with factory() as db:
        sub_id = (await db.execute(select(WebhookSubscription.id))).scalar_one()
        with pytest.raises(HTTPException) as exc:
            await webhooks.update_subscription(
                sub_id, WebhookSubscriptionUpdate(agent_id=6), db=db,
                current_user=SimpleNamespace(user_id=1),
            )
    assert exc.value.status_code == 404
    subs = await _subs(factory)
    assert [(s.consumer, s.agent_id) for s in subs] == [("workflow", None)]


# --- Revue du 19/10 -----------------------------------------------------------


async def test_a_workflow_subscription_does_not_cover_mails_received_before_it(
    factory, graph, monkeypatch
):
    """Le backlog agent se vide lentement : un mail arrivé AVANT l'abonnement
    workflow n'a été notifié qu'à l'agent. Le chemin agent doit encore lancer
    les workflows pour lui, sinon personne ne le fait."""
    _armed(monkeypatch, (ALICE, "outlook"))
    await wms.reconcile_outlook_subscriptions()
    async with factory() as db:
        created = (await db.execute(select(WebhookSubscription.created_at))).scalar_one()
        before = wms._aware(created) - timedelta(minutes=5)
        after = wms._aware(created) + timedelta(minutes=5)
        assert await wms.has_active_workflow_subscription(db, 1, received_at=before) is False
        assert await wms.has_active_workflow_subscription(db, 1, received_at=after) is True


async def test_agent_path_checks_coverage_at_the_mail_reception_time(monkeypatch):
    import apowerb.routers.webhook_handlers.outlook as ol

    received = datetime(2026, 10, 19, 8, 0, tzinfo=timezone.utc)
    log_row = _log(agent_id=6)
    log_row.created_at = received
    _wire(monkeypatch, ol, log_row=log_row, workflow_sub_active=False)

    await ol.process_webhook_log_row(7)

    check = ol.workflow_mail_subscriptions.has_active_workflow_subscription
    assert check.await_args.kwargs["received_at"] == received


async def test_a_retriggered_workflow_log_replays_its_stored_copy(monkeypatch):
    """Mail parti de la boîte, mais déjà capturé au premier passage : un
    retrigger relance les workflows depuis cette copie, il ne dit pas
    « succès » sans rien lancer."""
    import apowerb.routers.webhook_handlers.outlook as ol

    stored = [{"filename": "AR.pdf", "path": "/x/AR.pdf", "content_type": "application/pdf"}]
    log_row = _log(agent_id=None)
    log_row.email_subject = "AR commande 42"
    log_row.email_sender = "fournisseur@acme.fr"
    log_row.email_body_text = "Bonjour"
    log_row.attachments = stored
    log_row.created_at = datetime(2026, 10, 19, 8, 0, tzinfo=timezone.utc)
    gone = AsyncMock(side_effect=RuntimeError("HTTP 404 ErrorItemNotFound"))
    dispatched, ran = _wire(monkeypatch, ol, log_row=log_row, workflow_sub_active=True, fetch=gone)

    out = await ol.process_webhook_log_row(7)

    ran.assert_not_awaited()
    dispatched.assert_awaited_once()
    kw = dispatched.await_args.kwargs
    assert kw["stored_attachments"] == stored
    env = kw["envelope"]
    assert (env["from"], env["subject"], env["body"]) == (
        "fournisseur@acme.fr", "AR commande 42", "Bonjour",
    )
    assert env["received_at"] == "2026-10-19T08:00:00+00:00"
    assert "REPLAY" in out


async def test_the_agent_delete_route_cannot_touch_the_workflow_subscription(
    factory, graph, monkeypatch
):
    from fastapi import HTTPException

    from apowerb.routers import webhooks

    _armed(monkeypatch, (ALICE, "outlook"))
    await wms.reconcile_outlook_subscriptions()
    async with factory() as db:
        sub_id = (await db.execute(select(WebhookSubscription.id))).scalar_one()
        with pytest.raises(HTTPException) as exc:
            await webhooks.delete_subscription(
                sub_id, db=db, current_user=SimpleNamespace(user_id=1)
            )
    assert exc.value.status_code == 404
    assert len(await _subs(factory)) == 1
    assert graph["delete"] == []


def test_a_failing_consumer_migration_still_creates_the_unique_index():
    from unittest.mock import MagicMock, patch

    import apowerb.helpers.webhook_migration as mod

    conn = MagicMock()
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value = conn
    inspector = MagicMock()
    inspector.get_columns.return_value = [{"name": "agent_id", "nullable": False}]
    with patch.object(mod, "create_engine", return_value=engine), patch.object(
        mod, "inspect", return_value=inspector
    ), patch.object(mod, "DBConfig") as cfg, patch.object(
        mod, "ensure_consumer_column", side_effect=RuntimeError("check violated")
    ):
        cfg.return_value.get_db_url.return_value = "postgresql+asyncpg://x/y"
        mod.ensure_webhook_subscriptions_columns()
    sql = " ".join(str(c.args[0]) for c in conn.execute.call_args_list)
    assert "ux_webhook_subscriptions_workflow_owner" in sql

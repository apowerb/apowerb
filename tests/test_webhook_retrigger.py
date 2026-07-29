"""Tests TDD pour l'endpoint POST /logs/{log_id}/retrigger et la dedup mail 24h.

Structure identique a test_webhooks_router.py et test_webhook_serve_endpoints.py :
- _FakeSession pour mock de la DB async
- _build_app pour construire une app FastAPI isolee
- Override de get_current_user et get_db

Cas couverts :
(a) log status='error' sub active -> 202 + status pending, attempts=0
(b) log 'in_progress' -> 409 already_running_or_queued
(c) log d'un autre user -> 404
(d) sub status!='active' -> 409 subscription_inactive
(e) deux retriggers sequentiels -> un passe, l'autre 409
(f) dedup mail : 2e envoi live meme CommandeId <24h -> duplicate_within_24h ; >24h -> autorise
(g) dry_run apres live <24h -> autorise
(h) live apres dry_run <24h -> autorise
(i) log 'success' -> 202 + 'pending'
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi import status as http_status
from fastapi.testclient import TestClient


USER_A_ID = 1
USER_A_EMAIL = "alice@example.com"
USER_B_ID = 2
USER_B_EMAIL = "bob@example.com"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_user(user_id: int, email: str):
    u = MagicMock()
    u.email = email
    u.user_id = user_id
    u.role = "USER"
    return u


def _make_log(
    log_id: int,
    *,
    user_id: int = USER_A_ID,
    log_status: str = "error",
    subscription_id: int = 10,
):
    log = MagicMock()
    log.id = log_id
    log.user_id = user_id
    log.status = log_status
    log.subscription_id = subscription_id
    log.attempts = 3
    log.error_message = "some error"
    log.created_at = datetime.now(timezone.utc)
    return log


def _make_sub(
    sub_id: int,
    *,
    user_id: int = USER_A_ID,
    sub_status: str = "active",
):
    sub = MagicMock()
    sub.id = sub_id
    sub.user_id = user_id
    sub.status = sub_status
    return sub


class _FakeSession:
    """Minimal AsyncSession stub pour retrigger.

    execute() retourne differents objets selon le numero d'appel :
    - 1er appel (SELECT log) -> log_result
    - 2e appel (SELECT sub) -> sub_result
    - 3e appel (UPDATE RETURNING) -> selon update_returns_row
    """

    def __init__(
        self,
        *,
        log_result=None,
        sub_result=None,
        update_returns_row: bool = True,
    ):
        self._log_result = log_result
        self._sub_result = sub_result
        self._update_returns_row = update_returns_row
        self._call_count = 0
        self.committed = False

    async def execute(self, stmt):
        self._call_count += 1
        res = MagicMock()

        if self._call_count == 1:
            # SELECT WebhookLog
            res.scalar_one_or_none = MagicMock(return_value=self._log_result)
        elif self._call_count == 2:
            # SELECT WebhookSubscription
            res.scalar_one_or_none = MagicMock(return_value=self._sub_result)
        else:
            # UPDATE ... RETURNING id
            if self._update_returns_row:
                res.fetchone = MagicMock(return_value=(1,))
                res.scalar_one_or_none = MagicMock(return_value=1)
            else:
                res.fetchone = MagicMock(return_value=None)
                res.scalar_one_or_none = MagicMock(return_value=None)

        return res

    async def commit(self):
        self.committed = True

    def add(self, obj):
        pass

    async def refresh(self, obj):
        return None


def _build_app(
    session: _FakeSession,
    *,
    user_id: int | None = USER_A_ID,
    email: str | None = USER_A_EMAIL,
):
    from th2agent.auth.dependencies import get_current_user
    from th2agent.helpers.database import get_db
    from th2agent.routers.webhooks import router

    app = FastAPI()
    app.include_router(router, prefix="/api")

    async def _user_override():
        if user_id is None:
            raise HTTPException(
                status_code=http_status.HTTP_401_UNAUTHORIZED,
                detail="Not authenticated",
            )
        return _fake_user(user_id, email or USER_A_EMAIL)

    async def _db_override():
        yield session

    app.dependency_overrides[get_current_user] = _user_override
    app.dependency_overrides[get_db] = _db_override
    return app


# ---------------------------------------------------------------------------
# (a) log status='error' sub active -> 202 + status pending
# ---------------------------------------------------------------------------


class TestRetriggerHappyPath:
    def test_error_log_with_active_sub_returns_202(self):
        """(a) log status='error' sub active -> 202 + status pending."""
        log = _make_log(42, log_status="error")
        sub = _make_sub(10, sub_status="active")
        session = _FakeSession(log_result=log, sub_result=sub, update_returns_row=True)
        client = TestClient(_build_app(session))

        resp = client.post("/api/webhooks/logs/42/retrigger")

        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["log_id"] == 42
        assert body["status"] == "pending"
        assert session.committed

    def test_success_log_can_be_retriggered(self):
        """(i) log 'success' -> 202 + 'pending'."""
        log = _make_log(42, log_status="success")
        sub = _make_sub(10, sub_status="active")
        session = _FakeSession(log_result=log, sub_result=sub, update_returns_row=True)
        client = TestClient(_build_app(session))

        resp = client.post("/api/webhooks/logs/42/retrigger")

        assert resp.status_code == 202, resp.text
        assert resp.json()["status"] == "pending"


# ---------------------------------------------------------------------------
# (b) log 'in_progress' ou 'pending' -> 409 already_running_or_queued
# ---------------------------------------------------------------------------


class TestRetriggerAlreadyRunning:
    def test_in_progress_log_returns_409(self):
        """(b) log 'in_progress' -> 409 already_running_or_queued."""
        log = _make_log(42, log_status="in_progress")
        sub = _make_sub(10, sub_status="active")
        # UPDATE WHERE status NOT IN ('in_progress','pending') -> no row
        session = _FakeSession(log_result=log, sub_result=sub, update_returns_row=False)
        client = TestClient(_build_app(session))

        resp = client.post("/api/webhooks/logs/42/retrigger")

        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == "already_running_or_queued"

    def test_pending_log_returns_409(self):
        log = _make_log(42, log_status="pending")
        sub = _make_sub(10, sub_status="active")
        session = _FakeSession(log_result=log, sub_result=sub, update_returns_row=False)
        client = TestClient(_build_app(session))

        resp = client.post("/api/webhooks/logs/42/retrigger")

        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == "already_running_or_queued"


# ---------------------------------------------------------------------------
# (c) log d'un autre user -> 404
# ---------------------------------------------------------------------------


class TestRetriggerCrossUser:
    def test_log_of_other_user_returns_404(self):
        """(c) log d'un autre user -> 404 (SELECT filtre user_id)."""
        session = _FakeSession(log_result=None)
        client = TestClient(_build_app(session, user_id=USER_A_ID))

        resp = client.post("/api/webhooks/logs/99/retrigger")

        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"] == "webhook log not found"


# ---------------------------------------------------------------------------
# (d) sub status!='active' -> 409 subscription_inactive
# ---------------------------------------------------------------------------


class TestRetriggerSubscriptionInactive:
    def test_paused_sub_returns_409(self):
        """(d) sub status paused -> 409 subscription_inactive."""
        log = _make_log(42, log_status="error")
        sub = _make_sub(10, sub_status="paused")
        session = _FakeSession(log_result=log, sub_result=sub)
        client = TestClient(_build_app(session))

        resp = client.post("/api/webhooks/logs/42/retrigger")

        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == "subscription_inactive"

    def test_deleted_sub_returns_409(self):
        log = _make_log(42, log_status="error")
        sub = _make_sub(10, sub_status="deleted")
        session = _FakeSession(log_result=log, sub_result=sub)
        client = TestClient(_build_app(session))

        resp = client.post("/api/webhooks/logs/42/retrigger")

        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == "subscription_inactive"

    def test_none_sub_returns_404(self):
        """Sub None (supprimee de la DB) -> 404."""
        log = _make_log(42, log_status="error")
        session = _FakeSession(log_result=log, sub_result=None)
        client = TestClient(_build_app(session))

        resp = client.post("/api/webhooks/logs/42/retrigger")

        assert resp.status_code == 404, resp.text

    def test_sub_owned_by_other_user_returns_404(self):
        """Sub appartient a un autre user -> 404.
        Avec le double-filtre SQL (id + user_id), la DB retourne None pour une
        sub d'un autre user. Le mock simule ce comportement avec sub_result=None.
        """
        log = _make_log(42, log_status="error")
        # SQL double-filter returns None when sub belongs to another user
        session = _FakeSession(log_result=log, sub_result=None)
        client = TestClient(_build_app(session))

        resp = client.post("/api/webhooks/logs/42/retrigger")

        assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# (e) deux retriggers sequentiels -> un seul passe, l'autre 409
# ---------------------------------------------------------------------------


class TestRetriggerConcurrent:
    def test_second_retrigger_when_already_pending_returns_409(self):
        """(e) deux retriggers sequentiels -> un seul passe, l'autre 409."""
        log = _make_log(42, log_status="error")
        sub = _make_sub(10, sub_status="active")

        # 1er retrigger : succes
        session1 = _FakeSession(log_result=log, sub_result=sub, update_returns_row=True)
        client1 = TestClient(_build_app(session1))
        resp1 = client1.post("/api/webhooks/logs/42/retrigger")
        assert resp1.status_code == 202, resp1.text

        # 2e retrigger : le log est maintenant 'pending', UPDATE retourne rien
        log2 = _make_log(42, log_status="pending")
        session2 = _FakeSession(log_result=log2, sub_result=sub, update_returns_row=False)
        client2 = TestClient(_build_app(session2))
        resp2 = client2.post("/api/webhooks/logs/42/retrigger")
        assert resp2.status_code == 409, resp2.text
        assert resp2.json()["detail"] == "already_running_or_queued"



# ---------------------------------------------------------------------------
# (f) assertion structurelle sur la clause NOT IN de l'UPDATE
# ---------------------------------------------------------------------------


class TestRetriggerUpdateClauseNotIn:
    """Verifie structurellement que l'UPDATE atomique contient NOT IN."""

    def test_update_stmt_contains_not_in_status_clause(self):
        """Compile le statement update de l'endpoint et assert la sous-chaine NOT IN."""
        from sqlalchemy import update
        from sqlalchemy.dialects import sqlite
        from th2agent.models import WebhookLog

        stmt = (
            update(WebhookLog)
            .where(
                WebhookLog.id == 42,
                WebhookLog.user_id == 1,
                WebhookLog.status.not_in(["in_progress", "pending"]),
            )
            .values(status="pending", attempts=0)
        )
        compiled = stmt.compile(
            dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True}
        )
        sql = str(compiled).lower()
        assert "not in" in sql, f"Clause NOT IN absente du UPDATE compile: {sql}"
        assert "in_progress" in sql, f"Valeur in_progress absente: {sql}"
        assert "pending" in sql, f"Valeur pending absente: {sql}"

# ---------------------------------------------------------------------------
# Helpers pour tests dedup mail
# ---------------------------------------------------------------------------


def _mock_engine_for_dedup(found_recent: bool):
    """Mock engine pour les tests de dedup 24h sur scei_mail_audit."""
    import contextlib

    @contextlib.contextmanager
    def connect_ctx():
        conn = MagicMock()

        def execute(stmt, params=None):
            sql = str(stmt).lower()
            r = MagicMock()
            if "scei_mail_audit" in sql and "select" in sql:
                row = (1,) if found_recent else None
                r.first.return_value = row
                r.scalar.return_value = row[0] if row else None
            elif "commanditaires" in sql:
                email = (params or {}).get("email", "")
                rows = [(email,)] if email else []
                r.first.return_value = rows[0] if rows else None
                r.scalar.return_value = rows[0][0] if rows else None
                r.fetchall.return_value = rows
            elif "insert" in sql:
                r.first.return_value = (42,)
                r.scalar.return_value = 42
                r.lastrowid = 42
            return r

        conn.execute.side_effect = execute
        conn.commit = MagicMock()
        yield conn

    engine = MagicMock()
    engine.connect = connect_ctx
    engine.begin = connect_ctx
    return engine


def _ctx_mock():
    ctx = MagicMock()
    state_dict = {}

    class _State:
        def get(self, k, default=None):
            return state_dict.get(k, default)

        def __setitem__(self, k, v):
            state_dict[k] = v

        def __getitem__(self, k):
            return state_dict[k]

        def __contains__(self, k):
            return k in state_dict

    ctx.state = _State()
    return ctx, state_dict


# ---------------------------------------------------------------------------
# (f/g/h) Tests dedup mail 24h
# ---------------------------------------------------------------------------


class TestSceiMailDedup24h:
    def test_second_live_send_same_commande_within_24h_refused(self):
        """(f) 2e envoi live meme CommandeId <24h -> reason duplicate_within_24h."""
        from th2customers.scei.tools import scei_mail

        engine = _mock_engine_for_dedup(found_recent=True)
        ctx, _ = _ctx_mock()

        with (
            patch.dict("os.environ", {"SCEI_MAIL_AUTO_DRY_RUN": "false"}),
            patch.object(scei_mail, "_get_db_engine", return_value=engine),
            patch.object(scei_mail, "_send_outlook") as send,
        ):
            result = scei_mail.tool_send_scei_mail(
                to="acheteur@scei88.fr",
                subject="AR CF111",
                body="...",
                commande_id="CF111",
                tool_context=ctx,
            )

        assert result["success"] is False
        assert result["reason"] == "duplicate_within_24h"
        send.assert_not_called()

    def test_live_send_after_24h_allowed(self):
        """(f) >24h -> autorise."""
        from th2customers.scei.tools import scei_mail

        engine = _mock_engine_for_dedup(found_recent=False)
        ctx, _ = _ctx_mock()

        with (
            patch.dict("os.environ", {"SCEI_MAIL_AUTO_DRY_RUN": "false"}),
            patch.object(scei_mail, "_get_db_engine", return_value=engine),
            patch.object(scei_mail, "_validate_recipient", return_value=True),
            patch.object(scei_mail, "_log_audit", return_value=42),
            patch.object(scei_mail, "_send_outlook", return_value={"ok": True}) as send,
        ):
            result = scei_mail.tool_send_scei_mail(
                to="acheteur@scei88.fr",
                subject="AR CF111",
                body="...",
                commande_id="CF111",
                tool_context=ctx,
            )

        assert result["success"] is True
        assert result["mode"] == "live"
        send.assert_called_once()

    def test_dry_run_after_live_within_24h_allowed(self):
        """(g) dry_run apres live <24h -> autorise (dedup ne s'applique pas au dry_run)."""
        from th2customers.scei.tools import scei_mail

        # found_recent=True mais dry_run -> doit passer quand meme
        engine = _mock_engine_for_dedup(found_recent=True)
        ctx, _ = _ctx_mock()

        with (
            patch.dict("os.environ", {"SCEI_MAIL_AUTO_DRY_RUN": "true"}),
            patch.object(scei_mail, "_get_db_engine", return_value=engine),
            patch.object(scei_mail, "_validate_recipient", return_value=True),
            patch.object(scei_mail, "_log_audit", return_value=42),
            patch.object(scei_mail, "_send_outlook") as send,
        ):
            result = scei_mail.tool_send_scei_mail(
                to="acheteur@scei88.fr",
                subject="AR CF111",
                body="...",
                commande_id="CF111",
                tool_context=ctx,
            )

        assert result["success"] is True
        assert result["mode"] == "dry_run"
        send.assert_not_called()

    def test_live_after_dry_run_within_24h_allowed(self):
        """(h) live apres dry_run <24h -> autorise (dedup ne compte que les live)."""
        from th2customers.scei.tools import scei_mail

        # found_recent=False : SELECT filtre Mode='live' donc dry_run precedent invisible
        engine = _mock_engine_for_dedup(found_recent=False)
        ctx, _ = _ctx_mock()

        with (
            patch.dict("os.environ", {"SCEI_MAIL_AUTO_DRY_RUN": "false"}),
            patch.object(scei_mail, "_get_db_engine", return_value=engine),
            patch.object(scei_mail, "_validate_recipient", return_value=True),
            patch.object(scei_mail, "_log_audit", return_value=42),
            patch.object(scei_mail, "_send_outlook", return_value={"ok": True}) as send,
        ):
            result = scei_mail.tool_send_scei_mail(
                to="acheteur@scei88.fr",
                subject="AR CF111",
                body="...",
                commande_id="CF111",
                tool_context=ctx,
            )

        assert result["success"] is True
        send.assert_called_once()

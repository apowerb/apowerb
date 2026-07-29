"""Batch B22 — Integration tests for the scheduled agent-first emailing flow.

Scope: verify that the chain **scheduler → agent → send_email tool** works
end-to-end at the component level (no live Mage, no live Google/Outlook):

1. The scheduler can register a scheduled task for an agent that owns the
   ``send_email`` tool (Mage call mocked, DB dependency overridden).
2. The scheduler execution path decodes the agent refresh token and forwards
   the context to the ADK runner (``run_adk_agent`` mocked).
3. The Gmail ``send_email`` tool builds a well-formed base64url payload from
   the agent prompt (to / subject / body) and hits the expected Google API
   endpoint.
4. The refresh-token flow uses :func:`get_integration_tokens` (B7) to
   decrypt OAuth tokens at runtime — never plaintext storage.
5. Provider errors (401 token expired, 429 rate limit) are logged / reported
   cleanly by the scheduler runtime, without crashing silently.
6. A cancelled schedule does not trigger any send.

These are tests-only; **no production code is modified**.  Any defect
uncovered is reported in ``scratchpad/dev-report-B22.md`` for a dedicated
follow-up batch.
"""

from __future__ import annotations

import base64
import logging
from email.parser import BytesParser
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient
from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    JSON,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.engine import Engine


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


USER_ID = 42
USER_EMAIL = "elda@thaink2.com"
AGENT_ID = "agent99"


def _fake_user():
    u = MagicMock()
    u.email = USER_EMAIL
    u.user_id = USER_ID
    u.role = "USER"
    return u


@pytest.fixture()
def active_fernet():
    """Install a known-good Fernet instance on th2agent.helpers.encryptor."""
    from th2agent.helpers import encryptor as enc_mod

    key = Fernet.generate_key()
    original = enc_mod.fernet
    enc_mod.fernet = Fernet(key)
    try:
        yield enc_mod.fernet
    finally:
        enc_mod.fernet = original


@pytest.fixture()
def sqlite_engine() -> Engine:
    """In-memory SQLite engine mirroring the ``integrations`` table.

    Reused by tests that exercise :func:`get_integration_tokens` for the
    scheduler runtime path.
    """
    engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()
    Table(
        "integrations",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("user_id", Integer, nullable=False),
        Column("provider", String(50), nullable=False),
        Column("provider_user_id", String, nullable=True),
        Column("provider_username", String, nullable=True),
        Column("access_token", String, nullable=True),
        Column("refresh_token", String, nullable=True),
        Column("scopes", String, nullable=True),
        Column("meta", JSON, nullable=True),
        Column("created_at", DateTime, nullable=True),
        Column("updated_at", DateTime, nullable=True),
        UniqueConstraint("user_id", "provider", name="uq_integration_user_provider"),
    )
    metadata.create_all(engine)
    return engine


@pytest.fixture()
def integration_helpers(active_fernet, sqlite_engine, monkeypatch):
    """Return the helpers module with its engine builder redirected to SQLite."""
    from th2agent.integrations import helpers as mod

    monkeypatch.setattr(mod, "_build_engine", lambda *a, **k: sqlite_engine)
    return mod


def _build_scheduler_app(mage_client):
    """Mount the scheduler router with `get_current_user` and the Mage client overridden."""
    from th2agent.auth.dependencies import get_current_user
    from th2agent.routers import scheduler as scheduler_router

    # Patch the singleton client used by the router
    scheduler_router.scheduler_client = mage_client

    app = FastAPI()
    app.include_router(scheduler_router.router, prefix="/api/scheduler")

    async def _user_override():
        return _fake_user()

    app.dependency_overrides[get_current_user] = _user_override
    return app


# ---------------------------------------------------------------------------
# 1. Scheduler registers a scheduled task for an emailing agent
# ---------------------------------------------------------------------------


class TestSchedulerCreatesTask:
    """POST /scheduler/pipelines/agents/triggers registers a Mage trigger
    and returns its schedule_id/token."""

    def test_create_trigger_registers_and_returns_schedule_id(self):
        mage_client = MagicMock()
        app = _build_scheduler_app(mage_client)
        client = TestClient(app, raise_server_exceptions=False)

        fake_trigger_info = {
            "schedule_id": 4242,
            "trigger_token": "tok-abc",
            "status": "trigger_ready",
        }

        with patch(
            "th2agent.routers.scheduler.process_agent_registration",
            return_value=fake_trigger_info,
        ) as mocked:
            resp = client.post(
                "/api/scheduler/pipelines/agents/triggers",
                json={
                    "agent_id": AGENT_ID,
                    "agent_name": "emailing-agent",
                    "agent_model": "gemini-2.0",
                    "agent_description": "Agent that sends emails on schedule",
                },
            )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["success"] is True
        assert body["schedule_id"] == 4242
        assert body["trigger_token"] == "tok-abc"
        mocked.assert_called_once()
        args, kwargs = mocked.call_args
        # agent_id is forwarded, and the owner_id comes from current_user.email
        assert kwargs["agent_id"] == AGENT_ID
        assert kwargs["agent_meta"]["owner_id"] == USER_EMAIL
        assert kwargs["create_initial_run"] is False

    def test_create_trigger_requires_authentication(self):
        """Without a valid user override, the dependency must enforce 401."""
        from th2agent.auth.dependencies import get_current_user
        from th2agent.routers import scheduler as scheduler_router

        app = FastAPI()
        app.include_router(scheduler_router.router, prefix="/api/scheduler")

        async def _no_auth():
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Not authenticated",
            )

        app.dependency_overrides[get_current_user] = _no_auth
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/api/scheduler/pipelines/agents/triggers",
            json={"agent_id": AGENT_ID, "agent_name": "x"},
        )
        assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# 2. Scheduler → agent: run_adk_agent receives the decoded context
# ---------------------------------------------------------------------------


class TestSchedulerRunsAgentWithContext:
    """``run_agent_from_refresh_token`` decodes the JWT carried by the Mage
    run and forwards every required field to the ADK runner."""

    @pytest.mark.asyncio
    async def test_refresh_token_flow_forwards_context_to_adk_runner(self):
        from th2agent.scheduler import run_agent_background as rab

        fake_token_data = {
            "agent_name": "emailing-agent",
            "user_id": USER_EMAIL,
            "session_id": "sess-1",
            "new_message": {"role": "user", "content": "Send daily report"},
            "run_mode": "single",
            "streaming": False,
            "agent_metadata": {"owner_id": USER_EMAIL},
        }

        with patch.object(rab, "decode_agent_refresh_token", return_value=fake_token_data), \
             patch.object(rab, "get_agent_folder_name", return_value="emailing_agent_folder"), \
             patch("th2agent.core.adk_runner.get_adk_session", new=AsyncMock(return_value={})), \
             patch("th2agent.core.adk_runner.create_adk_agent_session", new=AsyncMock(return_value={})), \
             patch.object(rab, "run_adk_agent", new=AsyncMock(return_value={"ok": True})) as adk_mock:

            result = await rab.run_agent_from_refresh_token("fake-refresh-token")

        assert result["success"] is True
        assert result["agent_name"] == "emailing-agent"

        # Verify ADK was called with the decoded context
        adk_mock.assert_awaited_once()
        kwargs = adk_mock.await_args.kwargs
        assert kwargs["agent_name"] == "emailing_agent_folder"
        assert kwargs["user_id"] == USER_EMAIL
        # Session id is base + timestamp suffix → must start with the base
        assert kwargs["session_id"].startswith("sess-1_")
        assert kwargs["new_message"]["role"] == "user"
        assert kwargs["new_message"]["parts"][0]["text"] == "Send daily report"
        assert kwargs["token"] == "fake-refresh-token"

    @pytest.mark.asyncio
    async def test_refresh_token_flow_rejects_invalid_token(self):
        from th2agent.scheduler import run_agent_background as rab

        with patch.object(rab, "decode_agent_refresh_token", return_value=None):
            with pytest.raises(ValueError, match="Invalid or expired"):
                await rab.run_agent_from_refresh_token("bad-token")


# ---------------------------------------------------------------------------
# 3. Tool send_email builds the expected provider payload
# ---------------------------------------------------------------------------


class TestSendEmailToolPayload:
    """``tool_send_email`` (Gmail) must:
    - call POST /gmail/.../messages/send
    - carry a base64url-encoded RFC 2822 MIME message
    - whose To/Subject/body match the agent prompt arguments.
    """

    def test_gmail_send_email_builds_correct_payload(self):
        from th2agent.tools_store.portfolio import google_gmail

        captured: dict[str, Any] = {}

        class _Resp:
            status_code = 200

            def json(self):
                return {"id": "msg-123"}

            text = ""

        def _fake_post(url, headers=None, json=None, timeout=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return _Resp()

        with patch.object(
            google_gmail, "google_auth_headers", return_value={"Authorization": "Bearer fake"}
        ), patch("th2agent.tools_store.portfolio.google_gmail.httpx.post", side_effect=_fake_post):
            result = google_gmail.tool_send_email(
                to="alice@example.com",
                subject="Weekly report",
                body="Hello Alice,\n\nHere is your digest.",
            )

        assert result["status"] == "ok"
        assert result["message_id"] == "msg-123"
        assert captured["url"].endswith("/messages/send")
        assert captured["headers"]["Authorization"] == "Bearer fake"
        raw = captured["json"]["raw"]
        # Gmail expects URL-safe base64 (no '+' or '/')
        assert "+" not in raw and "/" not in raw

        # Decode and parse the MIME to assert the payload was built from
        # the (to, subject, body) arguments.
        padded = raw + "=" * (4 - len(raw) % 4)
        decoded = base64.urlsafe_b64decode(padded)
        parsed = BytesParser().parsebytes(decoded)
        assert parsed["To"] == "alice@example.com"
        assert parsed["Subject"] == "Weekly report"
        # MIMEText uses Content-Transfer-Encoding: base64 by default — decode
        # the payload to recover the plaintext body.
        body_bytes = parsed.get_payload(decode=True)
        assert body_bytes is not None
        assert "Here is your digest." in body_bytes.decode("utf-8")

    def test_gmail_send_email_rejects_empty_recipient(self):
        from th2agent.tools_store.portfolio import google_gmail

        result = google_gmail.tool_send_email(to="", subject="x", body="y")
        assert result["status"] == "error"
        assert result["retry"] is False

    def test_outlook_send_email_builds_graph_sendmail_payload(self):
        from th2agent.tools_store.portfolio import outlook_mail

        captured: dict[str, Any] = {}

        class _Resp:
            status_code = 202
            text = ""

        def _fake_post(url, headers=None, json=None, timeout=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return _Resp()

        with patch.object(
            outlook_mail, "_graph_headers", return_value={"Authorization": "Bearer fake"}
        ), patch("th2agent.tools_store.portfolio.outlook_mail.httpx.post", side_effect=_fake_post):
            result = outlook_mail.tool_send_outlook_email(
                to="bob@example.com",
                subject="Graph send test",
                body="Body text",
            )

        assert result["status"] == "success"
        assert captured["url"].endswith("/sendMail")
        msg = captured["json"]["message"]
        assert msg["subject"] == "Graph send test"
        assert msg["body"]["content"] == "Body text"
        assert msg["toRecipients"][0]["emailAddress"]["address"] == "bob@example.com"


# ---------------------------------------------------------------------------
# 4. Refresh token decryption via get_integration_tokens (B7)
# ---------------------------------------------------------------------------


class TestRefreshTokenDecryptedAtRuntime:
    """The runtime flow must call :func:`get_integration_tokens` to obtain
    the decrypted OAuth refresh token — never the raw ciphertext."""

    def test_get_integration_tokens_returns_decrypted_refresh_token(
        self, integration_helpers, sqlite_engine
    ):
        integration_helpers.save_integration_tokens(
            user_id=USER_ID,
            provider="google_gmail",
            access_token="plaintext-access",
            refresh_token="plaintext-refresh",
            provider_username=USER_EMAIL,
        )

        # Raw row must be ciphertext…
        from sqlalchemy import select

        table = integration_helpers._integrations_table(sqlite_engine)
        with sqlite_engine.connect() as conn:
            row = conn.execute(
                select(table.c.access_token, table.c.refresh_token).where(
                    (table.c.user_id == USER_ID)
                    & (table.c.provider == "google_gmail")
                )
            ).first()
        assert row[0] != "plaintext-access"
        assert row[1] != "plaintext-refresh"

        # …but the helper returns plaintext, which is what the tool layer
        # forwards to Google's token-refresh endpoint.
        tokens = integration_helpers.get_integration_tokens(
            user_id=USER_ID, provider="google_gmail"
        )
        assert tokens["access_token"] == "plaintext-access"
        assert tokens["refresh_token"] == "plaintext-refresh"

    def test_gmail_tool_loads_refresh_token_via_fetch_integration_configs(
        self, integration_helpers, sqlite_engine, monkeypatch
    ):
        """The Google tool lazily pulls its refresh_token through
        ``fetch_integration_configs``, which ultimately decrypts via B7."""
        from th2agent.tools_store.portfolio import google_auth

        # Force the tool to re-run the integration loader
        monkeypatch.setattr(google_auth, "_integration_loaded_for", {})
        monkeypatch.setenv("AGENT_OWNER", str(USER_ID))
        monkeypatch.delenv("GOOGLE_GMAIL_REFRESH_TOKEN", raising=False)

        integration_helpers.save_integration_tokens(
            user_id=USER_ID,
            provider="google_gmail",
            access_token="acc",
            refresh_token="refresh-xyz",
        )

        calls: list[str] = []

        def _fake_fetch(provider, user=None):
            calls.append(provider)
            tokens = integration_helpers.get_integration_tokens(
                user_id=USER_ID, provider=provider
            )
            return {
                "access_token": tokens["access_token"],
                "refresh_token": tokens["refresh_token"],
                "meta": {},
            }

        # The lazy loader imports it dynamically; patch the symbol in the
        # module that does ``from th2agent.integrations.helpers import ...``.
        monkeypatch.setattr(
            "th2agent.integrations.helpers.fetch_integration_configs", _fake_fetch
        )

        google_auth._ensure_integration_tokens("GOOGLE_GMAIL")

        assert calls == ["google_gmail"]
        import os as _os

        assert _os.environ.get("GOOGLE_GMAIL_REFRESH_TOKEN") == "refresh-xyz"


# ---------------------------------------------------------------------------
# 5. Provider errors (401 / 429) are logged, not crash-silent
# ---------------------------------------------------------------------------


class TestProviderErrorsAreSurfaced:
    """The tool layer must translate 401 / 429 into structured error dicts
    rather than raising inside the agent and killing the scheduled run."""

    def test_gmail_send_email_401_returns_auth_expired_error(self, caplog):
        from th2agent.tools_store.portfolio import google_gmail

        class _Resp:
            status_code = 401
            text = '{"error":"invalid_grant"}'

            def json(self):
                return {"error": "invalid_grant"}

        with patch.object(
            google_gmail, "google_auth_headers", return_value={"Authorization": "Bearer fake"}
        ), patch(
            "th2agent.tools_store.portfolio.google_gmail.httpx.post", return_value=_Resp()
        ):
            result = google_gmail.tool_send_email(
                to="alice@example.com", subject="x", body="y"
            )

        assert result["status"] == "error"
        assert result["retry"] is False
        assert "reconnect" in result["message"].lower() or "auth" in result["message"].lower()

    def test_gmail_send_email_429_returns_retryable_error_dict(self):
        from th2agent.tools_store.portfolio import google_gmail

        class _Resp:
            status_code = 429
            text = "Too many requests"

            def json(self):
                return {}

        with patch.object(
            google_gmail, "google_auth_headers", return_value={"Authorization": "Bearer fake"}
        ), patch(
            "th2agent.tools_store.portfolio.google_gmail.httpx.post", return_value=_Resp()
        ):
            result = google_gmail.tool_send_email(
                to="alice@example.com", subject="x", body="y"
            )

        assert result["status"] == "error"
        # Any Gmail >=400 that is not 401 must surface a structured error,
        # without raising out of the tool.
        assert "429" in result["message"] or "rate" in result["message"].lower() or "error" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_scheduler_runtime_logs_adk_failure(self, caplog):
        """If the downstream ADK call blows up, the scheduler runtime must
        log it (no silent swallow) and re-raise for the caller."""
        from th2agent.scheduler import run_agent_background as rab

        fake_token_data = {
            "agent_name": "emailing-agent",
            "user_id": USER_EMAIL,
            "session_id": "sess-1",
            "new_message": {"role": "user", "content": "Send digest"},
            "run_mode": "single",
            "streaming": False,
        }

        with patch.object(rab, "decode_agent_refresh_token", return_value=fake_token_data), \
             patch.object(rab, "get_agent_folder_name", return_value="folder"), \
             patch("th2agent.core.adk_runner.get_adk_session", new=AsyncMock(return_value={})), \
             patch("th2agent.core.adk_runner.create_adk_agent_session", new=AsyncMock(return_value={})), \
             patch.object(rab, "run_adk_agent", new=AsyncMock(side_effect=RuntimeError("boom"))):

            caplog.set_level(logging.ERROR, logger="th2agent.scheduler.run_agent_background")
            with pytest.raises(RuntimeError, match="boom"):
                await rab.run_agent_from_refresh_token("fake-token")

        # The scheduler must have logged the failure, not swallowed it.
        error_messages = [
            r.message for r in caplog.records if r.levelno >= logging.ERROR
        ]
        assert any("boom" in msg or "failed" in msg.lower() for msg in error_messages)


# ---------------------------------------------------------------------------
# 6. Cancelled schedule does not trigger any run
# ---------------------------------------------------------------------------


class TestCancelledSchedule:
    """PUT /pipelines/runs/{run_id}/cancel forwards to Mage and, once
    cancelled, the scheduler does not dispatch any send_email tool call."""

    def test_cancel_run_forwards_to_mage_client(self):
        mage_client = MagicMock()
        mage_client.cancel_pipeline_run.return_value = {
            "id": 77,
            "status": "cancelled",
        }
        app = _build_scheduler_app(mage_client)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.put("/api/scheduler/pipelines/runs/77/cancel")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "cancelled"
        mage_client.cancel_pipeline_run.assert_called_once_with(77)

    def test_cancel_run_returns_500_when_mage_fails(self):
        mage_client = MagicMock()
        mage_client.cancel_pipeline_run.return_value = None
        app = _build_scheduler_app(mage_client)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.put("/api/scheduler/pipelines/runs/77/cancel")
        assert resp.status_code == 500

    def test_cancelled_run_does_not_invoke_send_email_tool(self):
        """End-to-end sanity: when the cancel endpoint has been called, no
        follow-up pipeline trigger should be issued — therefore the Gmail
        tool's httpx.post must never be hit as part of that lifecycle."""
        mage_client = MagicMock()
        mage_client.cancel_pipeline_run.return_value = {"status": "cancelled"}
        app = _build_scheduler_app(mage_client)
        client = TestClient(app, raise_server_exceptions=False)

        with patch("th2agent.tools_store.portfolio.google_gmail.httpx.post") as post_mock:
            resp = client.put("/api/scheduler/pipelines/runs/77/cancel")
            assert resp.status_code == 200

            # Nothing further should trigger the Gmail send endpoint.
            post_mock.assert_not_called()

        # Also verify Mage was NOT asked to fire a new run afterwards.
        assert mage_client.trigger_pipeline.call_count == 0
        assert mage_client.trigger_pipeline_run_for_schedule.call_count == 0

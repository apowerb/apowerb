"""Tests for ``apowerb.routers.webhook_handlers._common.run_agent_for_webhook``.

Live regression 2026-05-11: webhook ADK sessions were stored under
``users/3/sessions/webhook_1`` (stringified user_id int) while the
frontend queries ADK with the authenticated user's email
(``users/com@scei88.fr/sessions/webhook_1``). Result: the operator
sees the "Webhook — scei_ar_assistant" conversation appear in the
sidebar but its content is empty, because ADK returns 404 on the
email-keyed lookup and the UI creates a new empty session in its
place.

These tests pin the contract: the ADK user_id passed to
``get_adk_session`` / ``create_adk_agent_session`` / ``run_adk_agent``
MUST be the User row's ``email`` field, not ``str(user_id)``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from apowerb.helpers.database import Base
from apowerb.models import User
from apowerb.routers.webhook_handlers import _common


@pytest.fixture
async def sqlite_with_user(monkeypatch):
    """sqlite + seeded User; sessionmanager.session() patched onto the
    ``_common`` module so ``run_agent_for_webhook`` reads from our DB."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    # Strip Postgres schema for sqlite compatibility (cross-schema CREATE
    # TABLE is unsupported).
    for t in Base.metadata.tables.values():
        t.schema = None
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


class TestAdkUserIdIsEmail:
    """The user_id passed to ADK MUST be the User's email so the
    frontend (which queries ADK by email) can retrieve the session."""

    @pytest.mark.asyncio
    async def test_run_agent_for_webhook_uses_email_as_adk_user_id(
        self, sqlite_with_user, monkeypatch
    ):
        captured: dict = {}

        async def fake_get_session(*, agent_name, user_id, session_id, token):
            captured["get_user_id"] = user_id
            # Simulate "session not found" so the create branch is hit
            raise RuntimeError("not found")

        async def fake_create_session(
            *, agent_name, user_id, session_id, data, token
        ):
            captured["create_user_id"] = user_id
            return {"id": session_id}

        async def fake_run(
            *, agent_name, user_id, session_id, new_message, run_mode, streaming, token
        ):
            captured["run_user_id"] = user_id
            return [{"content": {"parts": [{"text": "ok"}]}}]

        monkeypatch.setattr(_common, "get_adk_session", fake_get_session)
        monkeypatch.setattr(_common, "create_adk_agent_session", fake_create_session)
        monkeypatch.setattr(_common, "run_adk_agent", fake_run)

        await _common.run_agent_for_webhook(
            user_id=3,
            agent_id=6,
            sub_db_id=1,
            session_id="webhook_1",
            message_text="hello",
        )

        # All three ADK calls must be keyed by the email, not by the
        # stringified user_id integer.
        assert captured["get_user_id"] == "com@scei88.fr", (
            f"get_adk_session received user_id={captured['get_user_id']!r}; "
            "expected the User.email so the UI can find the session"
        )
        assert captured["create_user_id"] == "com@scei88.fr"
        assert captured["run_user_id"] == "com@scei88.fr"
        # And specifically NOT the stringified int — that was the bug.
        assert captured["run_user_id"] != "3"

    @pytest.mark.asyncio
    async def test_fresh_session_deletes_then_creates(
        self, sqlite_with_user, monkeypatch
    ):
        """fresh_session=True (Outlook/SCEI, session unique par webhook) ->
        delete_adk_agent_session AVANT create, get_adk_session JAMAIS
        (session fraiche par run -> anti accumulation historique -> 500)."""
        calls: list[str] = []

        async def fake_delete(*, agent_name, user_id, session_id, token):
            calls.append("delete")

        async def fake_get(*, agent_name, user_id, session_id, token):
            calls.append("get")

        async def fake_create(*, agent_name, user_id, session_id, data, token):
            calls.append("create")
            return {"id": session_id}

        async def fake_run(
            *, agent_name, user_id, session_id, new_message, run_mode, streaming, token
        ):
            return [{"content": {"parts": [{"text": "ok"}]}}]

        monkeypatch.setattr(_common, "delete_adk_agent_session", fake_delete)
        monkeypatch.setattr(_common, "get_adk_session", fake_get)
        monkeypatch.setattr(_common, "create_adk_agent_session", fake_create)
        monkeypatch.setattr(_common, "run_adk_agent", fake_run)

        await _common.run_agent_for_webhook(
            user_id=3, agent_id=12, sub_db_id=1,
            session_id="webhook_1", message_text="hello",
            fresh_session=True,
        )
        assert "delete" in calls, "fresh_session=True doit supprimer la session d'abord"
        assert "create" in calls
        assert "get" not in calls, "fresh_session=True ne doit PAS reutiliser (get)"
        assert calls.index("delete") < calls.index("create"), "delete avant create"

    @pytest.mark.asyncio
    async def test_default_reuses_session_for_gmail(
        self, sqlite_with_user, monkeypatch
    ):
        """fresh_session=False (defaut, Gmail multi-messages) -> get_adk_session
        (reuse), delete JAMAIS : preserve l'historique inter-messages."""
        calls: list[str] = []

        async def fake_delete(*, agent_name, user_id, session_id, token):
            calls.append("delete")

        async def fake_get(*, agent_name, user_id, session_id, token):
            calls.append("get")
            return {"id": session_id}  # session existe -> reuse, pas de create

        async def fake_create(*, agent_name, user_id, session_id, data, token):
            calls.append("create")
            return {"id": session_id}

        async def fake_run(
            *, agent_name, user_id, session_id, new_message, run_mode, streaming, token
        ):
            return [{"content": {"parts": [{"text": "ok"}]}}]

        monkeypatch.setattr(_common, "delete_adk_agent_session", fake_delete)
        monkeypatch.setattr(_common, "get_adk_session", fake_get)
        monkeypatch.setattr(_common, "create_adk_agent_session", fake_create)
        monkeypatch.setattr(_common, "run_adk_agent", fake_run)

        await _common.run_agent_for_webhook(
            user_id=3, agent_id=6, sub_db_id=1,
            session_id="webhook_1", message_text="hello",
        )
        assert "get" in calls, "defaut doit reutiliser (get) la session"
        assert "delete" not in calls, "defaut (Gmail) ne doit PAS supprimer la session"

    @pytest.mark.asyncio
    async def test_run_agent_for_webhook_raises_on_unknown_user(
        self, sqlite_with_user, monkeypatch
    ):
        """If the user_id does not resolve to a User row we must fail
        loudly rather than silently fall back to a stringified int —
        which is exactly what produced the unreachable-session bug."""

        async def boom(**kwargs):
            raise AssertionError("should not be reached")

        monkeypatch.setattr(_common, "get_adk_session", boom)
        monkeypatch.setattr(_common, "create_adk_agent_session", boom)
        monkeypatch.setattr(_common, "run_adk_agent", boom)

        with pytest.raises(RuntimeError, match="Cannot resolve email"):
            await _common.run_agent_for_webhook(
                user_id=99999,  # not in DB
                agent_id=6,
                sub_db_id=1,
                session_id="webhook_1",
                message_text="hello",
            )


class TestSourceForbidsStringifiedUserId:
    """Static guard: the literal ``user_id_str = str(user_id)`` pattern
    is exactly what caused the bug. Block it from coming back via a
    refactor that silently undoes the email lookup."""

    def test_source_does_not_stringify_user_id_for_adk(self):
        import inspect

        src = inspect.getsource(_common.run_agent_for_webhook)
        assert "str(user_id)" not in src, (
            "run_agent_for_webhook must not stringify the integer "
            "user_id when calling ADK — ADK is then keyed by an "
            "identifier the UI cannot reproduce. Resolve the user's "
            "email first and use that."
        )
        assert "user_row.email" in src or ".email" in src, (
            "run_agent_for_webhook must look up the User row and use "
            "its email as the ADK user_id"
        )

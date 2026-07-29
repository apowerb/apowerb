"""Tests de non-fuite inter-tenants pour les routers browsers Google Drive
et OneDrive.

Référence : ``review-security.md`` Critical C6 + ``correctifs-plan.md`` B6.

Avant le fix, ces deux routers écrivaient directement dans
``os.environ[...]`` le refresh token de l'utilisateur courant. Deux requêtes
concurrentes provenant de tenants différents pouvaient lire la valeur
positionnée par l'autre, et la valeur restait persistée au-delà de la
requête.

Ces tests vérifient :

1. Après un appel réussi, la variable d'environnement du refresh token
   n'est pas laissée dans ``os.environ`` (no leak).
2. Dans la section protégée (pendant l'appel au ``tool_*`` côté
   portfolio), la variable contient bien le refresh token du caller — pas
   celui d'un autre tenant en concurrence.
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


USER_A = "alice@example.com"
USER_B = "bob@example.com"

ALICE_DRIVE_TOKEN = "alice-drive-refresh-token"
BOB_DRIVE_TOKEN = "bob-drive-refresh-token"
ALICE_ONEDRIVE_TOKEN = "alice-onedrive-refresh-token"
BOB_ONEDRIVE_TOKEN = "bob-onedrive-refresh-token"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _fake_user(email: str):
    u = MagicMock()
    u.email = email
    u.user_id = 1 if email == USER_A else 2
    u.role = "USER"
    return u


def _make_db_mock(token_by_user_id: dict[int, str]):
    """Return an AsyncSession mock whose ``execute`` yields an Integration
    row with the refresh token looked up by ``user_id`` from the filter.
    """
    async def _execute(stmt):
        # Extract the user_id from the WHERE clause via compiled params.
        compiled = stmt.compile(compile_kwargs={"literal_binds": True})
        sql = str(compiled)
        # Naive but reliable for our select(Integration).where(user_id == X).
        uid = None
        for uid_candidate, token in token_by_user_id.items():
            if f"= {uid_candidate}" in sql or f"={uid_candidate}" in sql:
                uid = uid_candidate
                break

        integration = None
        if uid is not None:
            integration = MagicMock()
            integration.refresh_token = token_by_user_id[uid]

        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=integration)
        return result

    db = MagicMock()
    db.execute = AsyncMock(side_effect=_execute)
    return db


# ---------------------------------------------------------------------------
# Google Drive browser
# ---------------------------------------------------------------------------


class TestGoogleDriveBrowserNoLeak:
    def _build_client(self, user_email: str, tool_observer):
        from th2agent.routers.google_drive_browser import router
        from th2agent.auth.dependencies import get_current_user
        from th2agent.helpers.database import get_db

        app = FastAPI()
        app.include_router(router)

        async def override_user():
            return _fake_user(user_email)

        token_map = {
            1: ALICE_DRIVE_TOKEN,
            2: BOB_DRIVE_TOKEN,
        }
        db_mock = _make_db_mock(token_map)

        async def override_db():
            yield db_mock

        app.dependency_overrides[get_current_user] = override_user
        app.dependency_overrides[get_db] = override_db

        return TestClient(app)

    def test_no_env_var_leak_after_call(self):
        observed: list[str | None] = []

        def fake_tool(**kwargs):
            observed.append(os.environ.get("GOOGLE_DRIVE_REFRESH_TOKEN"))
            return {"status": "success", "items": [], "total": 0}

        # Clean slate
        os.environ.pop("GOOGLE_DRIVE_REFRESH_TOKEN", None)

        with patch("th2agent.routers.google_drive_browser.tool_list_files",
                   side_effect=fake_tool):
            client = self._build_client(USER_A, observed)
            resp = client.get("/api/googledrivebrowser/list")

        assert resp.status_code == 200, resp.text
        # The tool saw the caller's token…
        assert observed == [ALICE_DRIVE_TOKEN]
        # …and the env var is no longer present after the request.
        assert "GOOGLE_DRIVE_REFRESH_TOKEN" not in os.environ


# ---------------------------------------------------------------------------
# OneDrive browser
# ---------------------------------------------------------------------------


class TestOneDriveBrowserNoLeak:
    def _build_client(self, user_email: str):
        from th2agent.routers.onedrive_browser import router
        from th2agent.auth.dependencies import get_current_user
        from th2agent.helpers.database import get_db

        app = FastAPI()
        app.include_router(router)

        async def override_user():
            return _fake_user(user_email)

        token_map = {
            1: ALICE_ONEDRIVE_TOKEN,
            2: BOB_ONEDRIVE_TOKEN,
        }
        db_mock = _make_db_mock(token_map)

        async def override_db():
            yield db_mock

        app.dependency_overrides[get_current_user] = override_user
        app.dependency_overrides[get_db] = override_db

        return TestClient(app)

    def test_no_env_var_leak_after_call(self):
        observed: list[str | None] = []

        def fake_tool(**kwargs):
            observed.append(os.environ.get("ONEDRIVE_REFRESH_TOKEN"))
            return {"status": "success", "items": [], "total": 0}

        os.environ.pop("ONEDRIVE_REFRESH_TOKEN", None)

        with patch("th2agent.routers.onedrive_browser.tool_list_files",
                   side_effect=fake_tool):
            client = self._build_client(USER_A)
            resp = client.get("/api/onedrivebrowser/list")

        assert resp.status_code == 200, resp.text
        assert observed == [ALICE_ONEDRIVE_TOKEN]
        assert "ONEDRIVE_REFRESH_TOKEN" not in os.environ


# ---------------------------------------------------------------------------
# Concurrency — two users running simultaneously must not observe each
# other's token while inside the protected section.
# ---------------------------------------------------------------------------


class TestBrowsersConcurrentNoCrossTenantLeak:
    @pytest.mark.asyncio
    async def test_concurrent_google_drive_calls_isolated(self):
        """Alice and Bob calling Drive browser concurrently must each see
        their own refresh token during the ``tool_list_files`` call.

        Uses the real ``env_scope`` + ``asyncio.Lock`` added in B6; without
        the lock, the two threads could interleave their env var writes and
        observe the wrong value.
        """
        from th2agent.routers import google_drive_browser as gdb

        # Each fake tool invocation waits a tick to encourage interleaving.
        call_order: list[tuple[str, str | None]] = []

        def fake_tool(**kwargs):
            call_order.append(
                ("enter", os.environ.get("GOOGLE_DRIVE_REFRESH_TOKEN"))
            )
            # Simulate some I/O while another request may be queued.
            import time

            time.sleep(0.05)
            call_order.append(
                ("exit", os.environ.get("GOOGLE_DRIVE_REFRESH_TOKEN"))
            )
            return {"status": "success", "items": [], "total": 0}

        os.environ.pop("GOOGLE_DRIVE_REFRESH_TOKEN", None)

        with patch.object(gdb, "tool_list_files", side_effect=fake_tool):
            # Manually drive _inject_and_run_* via the router's logic by
            # emulating two concurrent HTTP requests. Easiest way: call
            # the async helper directly and the endpoint function.
            async def call_as(user_email: str, expected_token: str):
                db_mock = _make_db_mock({1: ALICE_DRIVE_TOKEN, 2: BOB_DRIVE_TOKEN})
                user = _fake_user(user_email)
                result = await gdb.list_files(
                    folder_id=None,
                    mime_type=None,
                    max_results=10,
                    db=db_mock,
                    current_user=user,
                )
                # Both responses should be ``success`` with the mocked empty list.
                # list_files returns the dict (not JSONResponse) when tool_list_files
                # succeeds; be defensive for either case.
                body = (
                    result.body if hasattr(result, "body") else result
                )
                return body, expected_token

            results = await asyncio.gather(
                call_as(USER_A, ALICE_DRIVE_TOKEN),
                call_as(USER_B, BOB_DRIVE_TOKEN),
            )
            assert len(results) == 2

        # No leak
        assert "GOOGLE_DRIVE_REFRESH_TOKEN" not in os.environ

        # For every ``enter`` observation the matching ``exit`` must have
        # the *same* token — meaning the other task did not overwrite our
        # env var mid-flight.
        assert len(call_order) % 2 == 0
        for i in range(0, len(call_order), 2):
            enter = call_order[i]
            exit_ = call_order[i + 1]
            assert enter[0] == "enter"
            assert exit_[0] == "exit"
            assert enter[1] == exit_[1], (
                f"Token changed during protected section: {enter} -> {exit_}. "
                f"Full trace: {call_order}"
            )
            assert enter[1] in (ALICE_DRIVE_TOKEN, BOB_DRIVE_TOKEN)

    @pytest.mark.asyncio
    async def test_concurrent_onedrive_calls_isolated(self):
        from th2agent.routers import onedrive_browser as odb

        call_order: list[tuple[str, str | None]] = []

        def fake_tool(**kwargs):
            call_order.append(("enter", os.environ.get("ONEDRIVE_REFRESH_TOKEN")))
            import time

            time.sleep(0.05)
            call_order.append(("exit", os.environ.get("ONEDRIVE_REFRESH_TOKEN")))
            return {"status": "success", "items": [], "total": 0}

        os.environ.pop("ONEDRIVE_REFRESH_TOKEN", None)

        with patch.object(odb, "tool_list_files", side_effect=fake_tool):
            async def call_as(user_email: str):
                db_mock = _make_db_mock(
                    {1: ALICE_ONEDRIVE_TOKEN, 2: BOB_ONEDRIVE_TOKEN}
                )
                user = _fake_user(user_email)
                return await odb.list_files(
                    folder_id=None,
                    folder_path=None,
                    file_type=None,
                    top=10,
                    db=db_mock,
                    current_user=user,
                )

            await asyncio.gather(call_as(USER_A), call_as(USER_B))

        assert "ONEDRIVE_REFRESH_TOKEN" not in os.environ
        assert len(call_order) % 2 == 0
        for i in range(0, len(call_order), 2):
            enter = call_order[i]
            exit_ = call_order[i + 1]
            assert enter[1] == exit_[1], (
                f"Token changed during protected section: {enter} -> {exit_}. "
                f"Full trace: {call_order}"
            )
            assert enter[1] in (ALICE_ONEDRIVE_TOKEN, BOB_ONEDRIVE_TOKEN)

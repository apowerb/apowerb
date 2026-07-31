"""Tests pour helpers/security.py download tokens.

Le download_token doit porter des claims `sub` (email) et `agent_id`
pour éviter qu'un token générique n'ouvre l'accès à n'importe quel fichier.
"""

import os
from pathlib import Path
import shutil
import tempfile
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apowerb.helpers.security import (
    generate_download_token,
    verify_download_token,
)


USER_A = "alice@example.com"
USER_B = "bob@example.com"


class TestDownloadTokenClaims:
    def test_generate_token_with_claims_is_verifiable(self):
        token = generate_download_token(
            sub=USER_A,
            agent_id="agent1",
            filename="file.txt",
        )
        payload = verify_download_token(token)
        # verify_download_token must now return the decoded payload (dict),
        # or at minimum expose sub/agent_id; truthy means "valid".
        assert payload, "Expected a valid payload for a scoped token"
        assert payload.get("sub") == USER_A
        assert payload.get("agent_id") == "agent1"

    def test_verify_rejects_token_without_sub(self):
        # A legacy/unscoped token (no sub, no agent_id) must be rejected.
        from datetime import datetime, timedelta, timezone
        from jose import jwt
        from apowerb.helpers.security import get_secret_key, get_algorithm

        expire = datetime.now(timezone.utc) + timedelta(minutes=5)
        bad_token = jwt.encode(
            {"exp": expire, "type": "download"},
            get_secret_key(),
            algorithm=get_algorithm(),
        )
        assert not verify_download_token(bad_token), (
            "Token without sub/agent_id must be rejected"
        )

    def test_verify_rejects_invalid_token(self):
        assert not verify_download_token("not-a-jwt")


class TestDownloadEndpointUsesTokenClaims:
    """Integration test: the /api/files/{agent_id}/{filename} endpoint must
    reject a download_token minted for a different agent_id."""

    @pytest.fixture()
    def app_and_dir(self):
        from apowerb.routers.files import router
        from apowerb.auth.dependencies import get_optional_user

        app = FastAPI()
        app.include_router(router, prefix="/api")

        # The download_token tests exercise the anonymous path (?token=...).
        # Override get_optional_user to always return None so FastAPI does not
        # try to hit the real auth stack / DB.
        async def no_user():
            return None

        app.dependency_overrides[get_optional_user] = no_user

        tmp_root = tempfile.mkdtemp(prefix="download_tok_")
        # Two files: one in agent1 (owned by USER_B), one in agent2 (USER_A)
        for agent_id, owner in (("agent1", USER_B), ("agent2", USER_A)):
            d = os.path.join(tmp_root, agent_id)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "hello.txt"), "w") as f:
                f.write(f"{agent_id}-{owner}-content")

        settings_mock = MagicMock()
        settings_mock.storage_mode = "local"

        with patch("apowerb.routers.files.uploads_dir", return_value=Path(tmp_root)), \
             patch("apowerb.routers.files.get_settings", return_value=settings_mock):
            yield app, tmp_root
        shutil.rmtree(tmp_root, ignore_errors=True)

    def test_download_token_cross_agent_rejected(self, app_and_dir):
        app, _ = app_and_dir
        client = TestClient(app)

        # Token minted for agent2 — must not grant access to agent1/hello.txt
        token = generate_download_token(
            sub=USER_A, agent_id="agent2", filename="hello.txt"
        )
        resp = client.get(f"/api/files/agent1/hello.txt?token={token}")
        assert resp.status_code == 403, resp.text

    def test_download_token_matching_agent_allowed(self, app_and_dir):
        app, _ = app_and_dir
        client = TestClient(app)

        token = generate_download_token(
            sub=USER_A, agent_id="agent2", filename="hello.txt"
        )
        resp = client.get(f"/api/files/agent2/hello.txt?token={token}")
        assert resp.status_code == 200, resp.text

    def test_download_token_cross_filename_rejected(self, app_and_dir):
        app, _ = app_and_dir
        client = TestClient(app)

        # Token minted for agent2/hello.txt — must not grant access to
        # any other filename in agent2.
        token = generate_download_token(
            sub=USER_A, agent_id="agent2", filename="hello.txt"
        )
        # Create another file in agent2
        other = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        )  # not used; we just trust the mint

        # The filename mismatch check is explicit
        token_for_other = generate_download_token(
            sub=USER_A, agent_id="agent2", filename="other.txt"
        )
        # Fetching hello.txt with an "other.txt" token must fail
        resp = client.get(f"/api/files/agent2/hello.txt?token={token_for_other}")
        assert resp.status_code == 403, resp.text

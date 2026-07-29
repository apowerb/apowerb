"""Tests IDOR pour routers/files.py.

Vérifient qu'un utilisateur authentifié ne peut pas lister/télécharger les
fichiers d'un agent qu'il ne possède pas.
"""

import os
from pathlib import Path
import shutil
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


USER_A = "alice@example.com"
USER_B = "bob@example.com"


def _fake_user(email: str):
    u = MagicMock()
    u.email = email
    u.user_id = 1 if email == USER_A else 2
    u.role = "USER"
    return u


@pytest.fixture()
def client():
    """TestClient authenticated as USER_A, where agent1 belongs to USER_B."""
    from th2agent.routers.files import router
    from th2agent.auth.dependencies import get_current_user, get_optional_user

    app = FastAPI()
    app.include_router(router, prefix="/api")

    async def override_user_a():
        return _fake_user(USER_A)

    app.dependency_overrides[get_current_user] = override_user_a
    app.dependency_overrides[get_optional_user] = override_user_a

    # Create a file for agent1 (owned by USER_B)
    tmp_root = tempfile.mkdtemp(prefix="files_idor_")
    try:
        agent_dir = os.path.join(tmp_root, "agent1")
        os.makedirs(agent_dir, exist_ok=True)
        with open(os.path.join(agent_dir, "secret.txt"), "w") as f:
            f.write("bob's secret")

        async def deny_for_other_agents(agent_id, current_user):
            from fastapi import HTTPException
            # agent1 belongs to USER_B ; any call with user != USER_B → 403
            if agent_id == "agent1" and current_user.email != USER_B:
                raise HTTPException(status_code=403, detail="Not your agent")
            if agent_id == "agent2":
                # agent2 belongs to USER_A, always allowed
                return
            # Default: deny
            raise HTTPException(status_code=403, detail="Not your agent")

        settings_mock = MagicMock()
        settings_mock.storage_mode = "local"

        with patch("th2agent.routers.files._validate_agent_ownership",
                   new=AsyncMock(side_effect=deny_for_other_agents)), \
             patch("th2agent.routers.files.uploads_dir", return_value=Path(tmp_root)), \
             patch("th2agent.routers.files.get_settings", return_value=settings_mock):
            yield TestClient(app)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


class TestIdorListFiles:
    def test_list_rejects_foreign_agent(self, client):
        resp = client.get("/api/files/agent1")
        assert resp.status_code == 403, resp.text


class TestIdorDownloadFile:
    def test_download_rejects_foreign_agent_with_bearer(self, client):
        # Bearer-authenticated: the current_user is USER_A but agent1 is USER_B.
        resp = client.get(
            "/api/files/agent1/secret.txt",
            headers={"Authorization": "Bearer fake-bearer"},
        )
        assert resp.status_code == 403, resp.text


class TestIdorUploadFile:
    def test_upload_rejects_foreign_agent(self, client):
        resp = client.post(
            "/api/files/upload",
            data={"agent_id": "agent1"},
            files={"file": ("note.txt", b"hello", "text/plain")},
        )
        assert resp.status_code == 403, resp.text

    def test_upload_chunk_rejects_foreign_agent(self, client):
        resp = client.post(
            "/api/files/upload-chunk",
            data={
                "upload_id": "u1",
                "agent_id": "agent1",
                "chunk_index": "0",
                "total_chunks": "1",
                "filename": "note.txt",
            },
            files={"chunk": ("chunk0", b"hello", "application/octet-stream")},
        )
        assert resp.status_code == 403, resp.text

    def test_upload_complete_rejects_foreign_agent(self, client):
        resp = client.post(
            "/api/files/upload-complete",
            json={
                "upload_id": "u1",
                "agent_id": "agent1",
                "filename": "note.txt",
                "total_chunks": 1,
            },
        )
        assert resp.status_code == 403, resp.text

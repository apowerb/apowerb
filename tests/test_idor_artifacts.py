"""Tests IDOR pour routers/artifacts.py.

Vérifient qu'un utilisateur authentifié ne peut pas accéder aux artefacts
d'un autre utilisateur en manipulant `user_id` dans le path.
"""

import os
import shutil
import tempfile
from unittest.mock import MagicMock, patch

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
    """Build a TestClient where get_current_user returns USER_A."""
    from apowerb.routers.artifacts import router
    from apowerb.auth.dependencies import get_current_user

    app = FastAPI()
    app.include_router(router, prefix="/api")

    async def override_user_a():
        return _fake_user(USER_A)

    app.dependency_overrides[get_current_user] = override_user_a

    # Create a real artifact file belonging to USER_B to ensure the 403
    # is triggered by auth check, not by a missing file (which would be 404).
    tmp_root = tempfile.mkdtemp(prefix="artifacts_idor_")
    try:
        victim_dir = os.path.join(tmp_root, "agent1", USER_B, "session_1")
        os.makedirs(victim_dir, exist_ok=True)
        with open(os.path.join(victim_dir, "hello.py"), "w") as f:
            f.write('{"filename": "hello.py", "language": "python", "code": "print(1)"}')

        # La racine des artefacts était une constante de module figée à
        # l'import ; c'est désormais `_artifacts_dir()`, résolu à l'appel.
        with patch("apowerb.routers.artifacts._artifacts_dir", return_value=tmp_root):
            yield TestClient(app)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


class TestIdorListArtifacts:
    def test_list_rejects_cross_user(self, client):
        resp = client.get(f"/api/artifacts/agent1/{USER_B}/session_1")
        assert resp.status_code == 403, resp.text


class TestIdorGetArtifact:
    def test_get_rejects_cross_user(self, client):
        resp = client.get(f"/api/artifacts/agent1/{USER_B}/session_1/hello.py")
        assert resp.status_code == 403, resp.text


class TestIdorExecuteArtifact:
    def test_execute_rejects_cross_user(self, client):
        resp = client.post(
            f"/api/artifacts/agent1/{USER_B}/session_1/hello.py/execute",
            json={"args": [], "timeout": 5},
        )
        assert resp.status_code == 403, resp.text


class TestOwnUserAllowed:
    """Sanity: USER_A can still access their own artefacts."""

    def test_list_own_artifacts_ok(self, client):
        # User A has no artifacts yet, but the endpoint should not 403.
        resp = client.get(f"/api/artifacts/agent1/{USER_A}/session_1")
        assert resp.status_code == 200, resp.text

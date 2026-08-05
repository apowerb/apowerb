"""routers/files.py must write uploads as input artifacts on S3 -- same
bucket, same key scheme as the existing "output" artifacts (#28/#29), with
"input" swapped in for "output" (see S3ArtifactService).

Option C (David, 05/08): a known session_id scopes the upload to
`artifacts/{agent}/{session}/input/...`; an unknown one falls back to a
shared, agent-wide scope `artifacts/{agent}/_shared/input/...`. The upload
API never received session_id before this change, so this stays optional
and every existing caller keeps working unmodified.

Reading must keep working for what was already there before this PR:
  - the legacy S3 key `uploads/{agent}/{filename}` (still real: 454 objects
    on the dev bucket at the time of writing, verified read-only).
  - local disk `uploads/{agent}/{filename}` (David's counts: 214 dev / 60
    prod files that are staying put, no migration).

Uses the same FakeS3Client as tests/test_s3_artifact_service.py / the
artifacts router S3 tests -- no network call.
"""

from __future__ import annotations

import datetime
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apowerb.configs.settings import get_settings
from apowerb.storage import s3 as s3_storage
from tests.helpers.fake_s3 import FakeS3Client

USER = "testt@gmail.com"
AGENT = "agent1164"
SESSION = "session_1785833154778"


def _seed_legacy_s3_key(fake: FakeS3Client, agent: str, filename: str, body: bytes) -> None:
    """Mirrors a file uploaded before this change: `uploads/{agent}/{filename}`."""
    fake._objects[f"uploads/{agent}/{filename}"] = {
        "body": body,
        "content_type": "application/octet-stream",
        "metadata": {},
        "last_modified": datetime.datetime.now(datetime.timezone.utc),
    }


@pytest.fixture()
def s3_env(monkeypatch):
    fake = FakeS3Client()
    monkeypatch.setattr(s3_storage, "_get_s3_client", lambda: fake)

    settings = get_settings()
    monkeypatch.setattr(settings, "storage_mode", "S3", raising=False)
    monkeypatch.setattr(settings, "s3_bucket_name", "test-bucket", raising=False)
    monkeypatch.setattr(settings, "s3_access_key", "AK", raising=False)
    monkeypatch.setattr(settings, "s3_access_key_secret", "SK", raising=False)
    monkeypatch.setattr(settings, "s3_endpoint", "https://s3.example.com", raising=False)
    monkeypatch.setattr(settings, "s3_region", "gra", raising=False)

    from apowerb.auth.dependencies import get_current_user, get_optional_user
    from apowerb.routers.files import router

    app = FastAPI()
    app.include_router(router, prefix="/api")

    async def _user():
        u = MagicMock()
        u.email = USER
        u.user_id = 1
        u.role = "USER"
        return u

    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[get_optional_user] = _user

    from unittest.mock import AsyncMock, patch

    with patch("apowerb.routers.files._validate_agent_ownership", new=AsyncMock(return_value=None)):
        yield TestClient(app), fake


class TestUploadWritesInputArtifact:
    def test_upload_without_session_writes_to_shared_scope(self, s3_env):
        client, fake = s3_env
        resp = client.post(
            "/api/files/upload",
            data={"agent_id": AGENT},
            files={"file": ("note.txt", b"hello", "text/plain")},
        )
        assert resp.status_code == 200, resp.text
        expected_key = f"artifacts/{AGENT}/_shared/input/note.txt/0/note.txt"
        assert expected_key in fake._objects

    def test_upload_with_session_writes_to_session_scope(self, s3_env):
        client, fake = s3_env
        resp = client.post(
            "/api/files/upload",
            data={"agent_id": AGENT, "session_id": SESSION},
            files={"file": ("note.txt", b"hello", "text/plain")},
        )
        assert resp.status_code == 200, resp.text
        expected_key = f"artifacts/{AGENT}/{SESSION}/input/note.txt/0/note.txt"
        assert expected_key in fake._objects
        assert f"artifacts/{AGENT}/_shared/input/note.txt/0/note.txt" not in fake._objects


class TestListFilesReadsInputArtifacts:
    def test_lists_a_freshly_uploaded_file(self, s3_env):
        client, _ = s3_env
        client.post(
            "/api/files/upload",
            data={"agent_id": AGENT},
            files={"file": ("note.txt", b"hello", "text/plain")},
        )
        resp = client.get(f"/api/files/{AGENT}")
        assert resp.status_code == 200
        names = [f["filename"] for f in resp.json()["files"]]
        assert "note.txt" in names

    def test_lists_a_legacy_s3_upload(self, s3_env):
        client, fake = s3_env
        _seed_legacy_s3_key(fake, AGENT, "old-report.pdf", b"legacy body")
        resp = client.get(f"/api/files/{AGENT}")
        assert resp.status_code == 200
        names = [f["filename"] for f in resp.json()["files"]]
        assert "old-report.pdf" in names


class TestDownloadFileReadsInputArtifacts:
    def test_downloads_a_freshly_uploaded_file(self, s3_env):
        client, _ = s3_env
        client.post(
            "/api/files/upload",
            data={"agent_id": AGENT},
            files={"file": ("note.txt", b"hello world", "text/plain")},
        )
        resp = client.get(
            f"/api/files/{AGENT}/note.txt",
            headers={"Authorization": "Bearer fake-bearer"},
        )
        assert resp.status_code == 200
        assert resp.content == b"hello world"

    def test_falls_back_to_legacy_s3_key_when_not_an_input_artifact(self, s3_env):
        client, fake = s3_env
        _seed_legacy_s3_key(fake, AGENT, "old-report.pdf", b"legacy body")
        resp = client.get(
            f"/api/files/{AGENT}/old-report.pdf",
            headers={"Authorization": "Bearer fake-bearer"},
        )
        assert resp.status_code == 200
        assert resp.content == b"legacy body"

    def test_falls_back_to_local_disk_when_absent_from_s3(self, s3_env, monkeypatch):
        client, _ = s3_env
        tmp_root = tempfile.mkdtemp(prefix="files_disk_fallback_")
        try:
            agent_dir = os.path.join(tmp_root, AGENT)
            os.makedirs(agent_dir, exist_ok=True)
            with open(os.path.join(agent_dir, "disk-only.txt"), "wb") as f:
                f.write(b"still on disk")

            monkeypatch.setattr("apowerb.routers.files.uploads_dir", lambda: Path(tmp_root))

            resp = client.get(
                f"/api/files/{AGENT}/disk-only.txt",
                headers={"Authorization": "Bearer fake-bearer"},
            )
            assert resp.status_code == 200
            assert resp.content == b"still on disk"
        finally:
            shutil.rmtree(tmp_root, ignore_errors=True)

    def test_404_when_nowhere_to_be_found(self, s3_env):
        client, _ = s3_env
        resp = client.get(
            f"/api/files/{AGENT}/nope.txt",
            headers={"Authorization": "Bearer fake-bearer"},
        )
        assert resp.status_code == 404


class TestUploadCompleteWritesInputArtifact:
    def test_assembles_chunks_into_an_input_artifact(self, s3_env):
        client, fake = s3_env
        upload_id = "u1"
        client.post(
            "/api/files/upload-chunk",
            data={
                "upload_id": upload_id,
                "agent_id": AGENT,
                "chunk_index": "0",
                "total_chunks": "1",
                "filename": "chunked.txt",
            },
            files={"chunk": ("chunk0", b"chunked content", "application/octet-stream")},
        )
        resp = client.post(
            "/api/files/upload-complete",
            json={
                "upload_id": upload_id,
                "agent_id": AGENT,
                "filename": "chunked.txt",
                "total_chunks": 1,
                "session_id": SESSION,
            },
        )
        assert resp.status_code == 200, resp.text
        expected_key = f"artifacts/{AGENT}/{SESSION}/input/chunked.txt/0/chunked.txt"
        assert expected_key in fake._objects
        assert fake._objects[expected_key]["body"] == b"chunked content"

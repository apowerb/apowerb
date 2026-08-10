"""/bi/upload-csv must mirror the uploaded file into the artifact chain --
same key scheme as routers/files.py (artifacts/{app_name}/_shared/input/...,
see apowerb.artifacts.upload_mirror) -- without moving the BI storage the
csv_executor and DatabaseDataStore actually read (bi/data/{org}/{project}/
data/{file_id}.csv, unchanged).

BI has no agent_id, only organization_id/project_id: the mirror writes under
bi_artifact_app_name(organization_id), always to the "_shared" scope (BI
never carries a session_id).

Mirroring is best-effort: a mirror failure must not turn a successful CSV
upload into an error response.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apowerb.configs.settings import get_settings
from apowerb.storage import s3 as s3_storage
from tests.helpers.fake_s3 import FakeS3Client

USER = "dev@thaink2.com"
ORG = "thaink2.com"
CSV_BODY = b"name,age\nalice,30\nbob,25\n"


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

    from apowerb.auth.dependencies import get_current_user
    from apowerb.bi.data.upload_router import router
    from apowerb.helpers.database import get_db

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    async def _user():
        u = MagicMock()
        u.email = USER
        u.user_id = 1
        u.role = "USER"
        return u

    async def _db():
        yield MagicMock()

    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[get_db] = _db

    with patch(
        "apowerb.bi.data.upload_router.DatabaseDataStore"
    ) as fake_store_cls:
        fake_store_cls.return_value.save = AsyncMock(return_value=None)
        yield TestClient(app), fake


class TestUploadCsvMirrorsInputArtifact:
    def test_uploads_csv_and_writes_input_artifact(self, s3_env):
        client, fake = s3_env
        resp = client.post(
            "/api/v1/bi/upload-csv",
            data={"organization_id": ORG, "project_id": "thaink2"},
            files={"file": ("dataset.csv", CSV_BODY, "text/csv")},
        )
        assert resp.status_code == 200, resp.text

        expected_key = f"artifacts/bi-{ORG}/_shared/input/dataset.csv/0/dataset.csv"
        assert expected_key in fake._objects
        assert fake._objects[expected_key]["body"] == CSV_BODY

        # The business storage (bi/data/...) still receives the file too.
        assert resp.json()["key"].startswith(f"bi/data/{ORG}/thaink2/data/")

    def test_upload_still_succeeds_when_mirror_fails(self, s3_env):
        client, _ = s3_env
        with patch(
            "apowerb.bi.data.upload_router.mirror_as_input_artifact",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            resp = client.post(
                "/api/v1/bi/upload-csv",
                data={"organization_id": ORG, "project_id": "thaink2"},
                files={"file": ("dataset2.csv", CSV_BODY, "text/csv")},
            )
        assert resp.status_code == 200, resp.text

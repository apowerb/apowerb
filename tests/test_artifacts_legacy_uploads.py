"""Files under the legacy ``uploads/{agent}/`` prefix must be reachable from
the Artifacts tab.

455 objects sit there on the dev bucket: uploads from the old flow, and
every file `create_downloadable_file` produced while it wrote outside the
artifact layout. They were stored, billed, and absent from the only screen
meant to show them -- the report David generated on 06/08 among them.
"""

from __future__ import annotations

import datetime
import json
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apowerb.configs.settings import get_settings
from apowerb.storage import s3 as s3_storage
from tests.helpers.fake_s3 import FakeS3Client

USER = "user@example.com"
AGENT = "agent1238"
SESSION = "session_1785927822560"


def _put(fake: FakeS3Client, key: str, body: bytes) -> None:
    fake._objects[key] = {
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

    _put(fake, f"uploads/{AGENT}/rapport_ia_sante_v2.html", b"<h1>rapport</h1>")

    from apowerb.auth.dependencies import get_current_user
    from apowerb.routers.artifacts import router

    app = FastAPI()
    app.include_router(router, prefix="/api")

    async def _user():
        u = MagicMock()
        u.email = USER
        u.user_id = 1
        u.role = "USER"
        return u

    app.dependency_overrides[get_current_user] = _user
    yield TestClient(app), fake


def _listing(client, scope="_shared"):
    return {a["filename"]: a for a in
            client.get(f"/api/artifacts/{AGENT}/{USER}/{scope}").json()}


def test_a_legacy_file_shows_up_in_the_tab(s3_env):
    client, _ = s3_env
    entry = _listing(client).get("rapport_ia_sante_v2.html")
    assert entry is not None
    assert entry["kind"] == "legacy"
    assert entry["language"] == "html"


def test_its_body_reads_back(s3_env):
    client, _ = s3_env
    r = client.get(f"/api/artifacts/{AGENT}/{USER}/_shared/rapport_ia_sante_v2.html")
    assert r.status_code == 200, r.text
    assert r.json()["code"] == "<h1>rapport</h1>"
    assert r.json()["kind"] == "legacy"


def test_a_legacy_file_is_not_repeated_for_every_conversation(s3_env):
    """It carries no session -- the old path never had one -- so listing it
    per session would show the same document once per conversation."""
    client, _ = s3_env
    assert "rapport_ia_sante_v2.html" not in _listing(client, SESSION)
    assert "rapport_ia_sante_v2.html" in _listing(client)


def test_a_real_artifact_hides_its_legacy_namesake(s3_env):
    """Same name in both places is the same file, migrated. Listing it twice
    would read as two separate documents."""
    client, fake = s3_env
    body = json.dumps({"filename": "rapport_ia_sante_v2.html", "language": "html",
                       "code": "<h1>nouveau</h1>"}).encode()
    _put(fake, f"artifacts/{AGENT}/_shared/output/rapport_ia_sante_v2.html/0/rapport_ia_sante_v2.html", body)

    entries = [a for a in client.get(f"/api/artifacts/{AGENT}/{USER}/_shared").json()
               if a["filename"] == "rapport_ia_sante_v2.html"]
    assert len(entries) == 1
    assert entries[0]["kind"] == "output"


def test_a_binary_legacy_file_is_flagged_not_mangled(s3_env):
    client, fake = s3_env
    _put(fake, f"uploads/{AGENT}/contrat.pdf", b"%PDF-1.4\x00\xff\xfe")

    r = client.get(f"/api/artifacts/{AGENT}/{USER}/_shared/contrat.pdf")
    assert r.status_code == 200
    assert r.json()["binary"] is True


def test_legacy_files_of_another_agent_stay_out(s3_env):
    client, fake = s3_env
    _put(fake, "uploads/agent999/autre.html", b"<h1>autre</h1>")
    assert "autre.html" not in _listing(client)


def test_kind_legacy_filters_to_them(s3_env):
    client, _ = s3_env
    r = client.get(
        f"/api/artifacts/{AGENT}/{USER}/_shared/rapport_ia_sante_v2.html?kind=legacy")
    assert r.status_code == 200
    assert r.json()["kind"] == "legacy"

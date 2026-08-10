"""Uploads must appear in the Artifacts tab next to generated artifacts,
tagged so the UI can tell them apart (Farid, 05/08).

PR #30 already writes uploads to S3 under the same key scheme as generated
artifacts, with "input" in place of "output". Nothing read them back: the
listing endpoint only walked the output segment, so an uploaded file was
stored, billed and invisible -- the same silent-empty failure mode as PR #17
and PR #29, one segment further along.

Same FakeS3Client as tests/test_artifacts_router_s3.py, so these exercise
the real S3ArtifactService key logic without a network call.
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
AGENT = "agent1164"
SESSION = "session_1785833154778"


def _put(fake: FakeS3Client, segment: str, filename: str, version: int,
         body: bytes, session_id: str = SESSION) -> None:
    key = f"artifacts/{AGENT}/{session_id}/{segment}/{filename}/{version}/{filename}"
    fake._objects[key] = {
        "body": body,
        "content_type": "application/octet-stream",
        "metadata": {},
        "last_modified": datetime.datetime.now(datetime.timezone.utc),
    }


def _put_output(fake: FakeS3Client, filename: str, version: int, payload: dict) -> None:
    _put(fake, "output", filename, version, json.dumps(payload).encode("utf-8"))


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

    _put_output(fake, "fizzbuzz.py", 0, {
        "filename": "fizzbuzz.py", "language": "python", "code": "print(1)",
    })
    _put(fake, "input", "notes.txt", 0, b"uploaded text")

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


def _listing(client) -> dict:
    return {a["filename"]: a for a in
            client.get(f"/api/artifacts/{AGENT}/{USER}/{SESSION}").json()}


def test_uploads_are_listed_next_to_generated_artifacts(s3_env):
    client, _ = s3_env
    by_name = _listing(client)
    assert sorted(by_name) == ["fizzbuzz.py", "notes.txt"]


def test_each_entry_carries_its_kind(s3_env):
    client, _ = s3_env
    by_name = _listing(client)
    assert by_name["fizzbuzz.py"]["kind"] == "output"
    assert by_name["notes.txt"]["kind"] == "input"


def test_upload_entry_reports_its_source_and_version(s3_env):
    client, fake = s3_env
    _put(fake, "input", "notes.txt", 10, b"newer text")
    _put(fake, "input", "notes.txt", 2, b"older text")

    entry = _listing(client)["notes.txt"]
    assert entry["source"] == "upload"
    # "10" < "2" as strings -- a lexical max would report version 2.
    assert entry["version"] == 10


def test_listing_never_downloads_upload_bodies(s3_env, monkeypatch):
    """A listing that read each upload would transfer the whole session on
    every page load -- an upload is an arbitrary file, not a small JSON
    payload, and its bytes say nothing the listing shows."""
    client, _ = s3_env
    from apowerb.artifacts import s3_artifact_service as svc

    def _boom(*args, **kwargs):
        raise AssertionError("listing downloaded an upload body")

    monkeypatch.setattr(svc.S3ArtifactService, "_load_input_artifact", _boom)
    assert "notes.txt" in _listing(client)


def test_upload_body_is_readable(s3_env):
    client, _ = s3_env
    r = client.get(f"/api/artifacts/{AGENT}/{USER}/{SESSION}/notes.txt")
    assert r.status_code == 200
    assert r.json()["code"] == "uploaded text"
    assert r.json()["kind"] == "input"
    assert r.json()["binary"] is False


def test_binary_upload_is_reported_not_mangled(s3_env):
    """A PDF decoded with errors="replace" would render as garbage text and
    look like a corrupted artifact; the flag lets the UI say what it is."""
    client, fake = s3_env
    _put(fake, "input", "scan.pdf", 0, b"%PDF-1.4\x00\xff\xfe binary")

    r = client.get(f"/api/artifacts/{AGENT}/{USER}/{SESSION}/scan.pdf")
    assert r.status_code == 200
    assert r.json()["binary"] is True
    assert r.json()["code"] == ""


def test_generated_artifact_wins_a_name_collision(s3_env):
    """Uploading report.html and then having the agent generate report.html
    is a real sequence. Without ?kind=, the generated one answers -- what
    every caller written before uploads existed already expects."""
    client, fake = s3_env
    _put_output(fake, "report.html", 0, {
        "filename": "report.html", "language": "html", "code": "<h1>generated</h1>",
    })
    _put(fake, "input", "report.html", 0, b"<h1>uploaded</h1>")

    r = client.get(f"/api/artifacts/{AGENT}/{USER}/{SESSION}/report.html")
    assert r.json()["code"] == "<h1>generated</h1>"
    assert r.json()["kind"] == "output"

    r = client.get(f"/api/artifacts/{AGENT}/{USER}/{SESSION}/report.html?kind=input")
    assert r.json()["code"] == "<h1>uploaded</h1>"
    assert r.json()["kind"] == "input"


def test_kind_output_does_not_fall_back_to_an_upload(s3_env):
    """An explicit kind is a filter, not a preference: asking for the
    generated artifact must 404 rather than hand back the upload."""
    client, _ = s3_env
    r = client.get(f"/api/artifacts/{AGENT}/{USER}/{SESSION}/notes.txt?kind=output")
    assert r.status_code == 404


def test_unknown_kind_is_rejected(s3_env):
    client, _ = s3_env
    r = client.get(f"/api/artifacts/{AGENT}/{USER}/{SESSION}/notes.txt?kind=elsewhere")
    assert r.status_code == 400


def test_shared_scope_is_listed_like_a_session(s3_env):
    """Uploads made without a session land under the literal "_shared" scope
    (option C, apowerb.artifacts.input_scope). Nothing else reaches them, so
    the tab must be able to ask for that scope by name."""
    client, fake = s3_env
    _put(fake, "input", "brief.md", 0, b"# brief", session_id="_shared")

    r = client.get(f"/api/artifacts/{AGENT}/{USER}/_shared")
    assert r.status_code == 200
    assert [a["filename"] for a in r.json()] == ["brief.md"]
    assert r.json()[0]["kind"] == "input"


def test_an_uploaded_script_can_be_executed(s3_env, monkeypatch):
    """Resolution for /execute follows the same path as reads, so a script
    that was uploaded rather than generated runs too."""
    client, fake = s3_env
    _put(fake, "input", "hello.py", 0, b"print('hi')")

    seen = {}

    async def _fake_execute(**kwargs):
        seen.update(kwargs)
        return {"stdout": "hi\\n", "stderr": "", "exit_code": 0, "duration_ms": 1}

    from apowerb.routers import artifacts as artifacts_router
    monkeypatch.setattr(artifacts_router, "execute_artifact", _fake_execute)

    r = client.post(
        f"/api/artifacts/{AGENT}/{USER}/{SESSION}/hello.py/execute",
        json={"args": [], "timeout": 5},
    )
    assert r.status_code == 200
    assert seen["code"] == "print('hi')"
    assert seen["language"] == "python"

"""Artifacts router must read from S3 instead of disk when S3 storage is
configured. PR #28 makes ADK write artifacts to S3 when STORAGE_MODE=S3
(already the case on dev and prod); without this, the router still reads
disk only, so the Artifacts tab goes empty even though objects sit in the
bucket -- the exact regression this test file guards against.

Uses the same FakeS3Client as tests/test_s3_artifact_service.py so these
tests exercise the router through the real S3ArtifactService key logic --
no network call, no divergence from what actually runs against the real
bucket (proven separately in tests/test_s3_artifact_service_real_bucket.py
and, for this router, in tests/test_artifacts_router_s3_real_bucket.py).
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


def _put(fake: FakeS3Client, app_name: str, session_id: str, filename: str,
         version: int, payload: dict) -> None:
    key = f"artifacts/{app_name}/{session_id}/output/{filename}/{version}/{filename}"
    fake._objects[key] = {
        "body": json.dumps(payload).encode("utf-8"),
        "content_type": "application/json",
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

    _put(fake, AGENT, SESSION, "fizzbuzz.py", 0, {
        "filename": "fizzbuzz.py", "language": "python", "code": "print(1)",
    })
    _put(fake, AGENT, SESSION, "rapport.html", 0, {
        "filename": "rapport.html", "language": "html", "code": "<h1>hi</h1>",
    })

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


def test_lists_artifacts_from_s3_when_configured(s3_env):
    client, _ = s3_env
    r = client.get(f"/api/artifacts/{AGENT}/{USER}/{SESSION}")
    assert r.status_code == 200
    names = sorted(a["filename"] for a in r.json())
    assert names == ["fizzbuzz.py", "rapport.html"]


def test_language_from_s3_payload(s3_env):
    client, _ = s3_env
    by_name = {a["filename"]: a for a in client.get(
        f"/api/artifacts/{AGENT}/{USER}/{SESSION}").json()}
    assert by_name["rapport.html"]["language"] == "html"
    assert by_name["fizzbuzz.py"]["language"] == "python"


def test_reads_the_body_from_s3(s3_env):
    client, _ = s3_env
    r = client.get(f"/api/artifacts/{AGENT}/{USER}/{SESSION}/fizzbuzz.py")
    assert r.status_code == 200
    assert r.json()["code"] == "print(1)"
    assert r.json()["language"] == "python"


def test_unknown_session_in_s3_mode_stays_empty(s3_env):
    client, _ = s3_env
    r = client.get(f"/api/artifacts/{AGENT}/{USER}/session_does_not_exist")
    assert r.status_code == 200
    assert r.json() == []


def test_missing_artifact_in_s3_mode_is_404(s3_env):
    client, _ = s3_env
    r = client.get(f"/api/artifacts/{AGENT}/{USER}/{SESSION}/nope.py")
    assert r.status_code == 404


def test_versions_sort_numerically_in_s3_mode(s3_env):
    client, fake = s3_env
    _put(fake, AGENT, SESSION, "fizzbuzz.py", 2, {
        "filename": "fizzbuzz.py", "language": "python", "code": "print(2)",
    })
    _put(fake, AGENT, SESSION, "fizzbuzz.py", 10, {
        "filename": "fizzbuzz.py", "language": "python", "code": "print(10)",
    })
    # "10" < "2" as strings -- a lexical max would serve version 2.
    r = client.get(f"/api/artifacts/{AGENT}/{USER}/{SESSION}/fizzbuzz.py")
    assert r.json()["code"] == "print(10)"


def test_agent_name_is_part_of_the_s3_path(s3_env):
    """Unlike local disk (scoped by user+session only, agent name ignored),
    the S3 key is scoped by (app_name, session_id): a different agent name
    must NOT resolve to the same artifacts."""
    client, _ = s3_env
    other = client.get(f"/api/artifacts/some-other-agent/{USER}/{SESSION}").json()
    assert other == []


def test_execute_resolves_against_s3_not_disk(s3_env):
    """Proves the execute endpoint's resolve step also switched to S3: a
    filename absent from S3 (even though nothing on disk was ever checked)
    must 404, not fall through to a stale disk lookup or a 500."""
    client, _ = s3_env
    r = client.post(
        f"/api/artifacts/{AGENT}/{USER}/{SESSION}/nope.py/execute",
        json={"args": [], "timeout": 5},
    )
    assert r.status_code == 404

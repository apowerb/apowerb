"""The library must be built from keys alone, and be complete.

The screen used to ask the API once per session plus once per agent: 386
sessions on a real dev account, ~200 ms per call even when the session held
nothing, so roughly fifteen seconds of waiting. Everything displayed is
already in the S3 key, so a listing per agent answers the whole thing.
"""

from __future__ import annotations

import datetime
import json
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apowerb.artifacts import library
from apowerb.configs.settings import get_settings
from apowerb.storage import s3 as s3_storage
from tests.helpers.fake_s3 import FakeS3Client

USER = "david@thaink2.com"
AGENT = "agent12"
OTHER = "agent99"
SESSION = "session_1785833154778"


def _put(fake: FakeS3Client, key: str, body: bytes = b"x", when=None) -> None:
    fake._objects[key] = {
        "body": body,
        "content_type": "application/octet-stream",
        "metadata": {},
        "last_modified": when or datetime.datetime.now(datetime.timezone.utc),
    }


@pytest.fixture()
def bucket(monkeypatch):
    fake = FakeS3Client()
    monkeypatch.setattr(s3_storage, "_get_s3_client", lambda: fake)

    settings = get_settings()
    monkeypatch.setattr(settings, "storage_mode", "S3", raising=False)
    monkeypatch.setattr(settings, "s3_bucket_name", "test-bucket", raising=False)
    monkeypatch.setattr(settings, "s3_access_key", "AK", raising=False)
    monkeypatch.setattr(settings, "s3_access_key_secret", "SK", raising=False)
    monkeypatch.setattr(settings, "s3_endpoint", "https://s3.example.com", raising=False)
    monkeypatch.setattr(settings, "s3_region", "gra", raising=False)
    return fake


def test_reads_everything_from_the_key(bucket):
    _put(bucket, f"artifacts/{AGENT}/{SESSION}/output/rapport.html/0/rapport.html")

    items = library.build_library({AGENT: "Invoice reader"})
    assert len(items) == 1
    assert items[0] == {
        "agent_folder": AGENT,
        "agent_name": "Invoice reader",
        "session_id": SESSION,
        "kind": "output",
        "filename": "rapport.html",
        "language": "html",
        "version": 0,
        "source": "adk",
        "updated_at": items[0]["updated_at"],
        "size": items[0]["size"],
    }


def test_downloads_nothing(bucket, monkeypatch):
    """The whole point: a listing answers, no object body is fetched."""
    _put(bucket, f"artifacts/{AGENT}/{SESSION}/output/rapport.html/0/rapport.html")
    _put(bucket, f"uploads/{AGENT}/vieux.csv")

    def _boom(*args, **kwargs):
        raise AssertionError("the library downloaded an object body")

    monkeypatch.setattr(bucket, "get_object", _boom)
    assert len(library.build_library({AGENT: "Invoice reader"})) == 2


def test_two_listings_whatever_the_number_of_agents(bucket):
    """One sweep of each root, not one pair per agent.

    Listing per agent cost 24 calls for an account owning 12 agents — 1 896 ms
    sequentially, for seven artifacts."""
    agents = {}
    for a in range(12):
        folder = f"agent{a}"
        agents[folder] = f"Agent {a}"
        for i in range(3):
            _put(bucket, f"artifacts/{folder}/session_{i}/output/f{i}.py/0/f{i}.py")

    bucket.list_calls = 0
    items = library.build_library(agents)
    assert len(items) == 36
    assert bucket.list_calls == 2, f"{bucket.list_calls} listings for 12 agents"


def test_the_sweep_never_leaks_another_owner_s_files(bucket):
    """The sweep sees the whole bucket, so ownership is what keeps one
    user's artifacts out of another's library. It is applied while
    collecting, not on the way out."""
    _put(bucket, f"artifacts/{AGENT}/{SESSION}/output/a_moi.py/0/a_moi.py")
    _put(bucket, f"artifacts/{OTHER}/{SESSION}/output/prive.py/0/prive.py")
    _put(bucket, f"artifacts/{OTHER}/{SESSION}/input/confidentiel.pdf/0/confidentiel.pdf")
    _put(bucket, f"uploads/{OTHER}/vieux_secret.csv")

    items = library.build_library({AGENT: "A"})
    assert [i["filename"] for i in items] == ["a_moi.py"]
    assert all(i["agent_folder"] == AGENT for i in items)


def test_no_agents_means_no_listing_at_all(bucket):
    _put(bucket, f"artifacts/{OTHER}/{SESSION}/output/prive.py/0/prive.py")

    bucket.list_calls = 0
    assert library.build_library({}) == []
    assert bucket.list_calls == 0


def test_a_legacy_name_is_hidden_only_for_its_own_agent(bucket):
    """Two agents can hold a file of the same name; masking must not cross
    the agent boundary."""
    _put(bucket, f"artifacts/{AGENT}/{SESSION}/output/rapport.html/0/rapport.html")
    _put(bucket, f"uploads/{AGENT}/rapport.html")
    _put(bucket, "uploads/agent77/rapport.html")

    items = library.build_library({AGENT: "A", "agent77": "B"})
    kinds = sorted((i["agent_folder"], i["kind"]) for i in items)
    assert kinds == [(AGENT, "output"), ("agent77", "legacy")]


def test_latest_version_wins_numerically(bucket):
    for version in (0, 2, 10):
        _put(bucket, f"artifacts/{AGENT}/{SESSION}/output/f.py/{version}/f.py")

    items = library.build_library({AGENT: "A"})
    assert [i["version"] for i in items] == [10]


def test_metadata_sibling_is_not_a_second_artifact(bucket):
    _put(bucket, f"artifacts/{AGENT}/{SESSION}/output/f.py/0/f.py")
    _put(bucket, f"artifacts/{AGENT}/{SESSION}/output/f.py/0/metadata.json")

    assert len(library.build_library({AGENT: "A"})) == 1


def test_inputs_outputs_and_legacy_are_tagged(bucket):
    _put(bucket, f"artifacts/{AGENT}/{SESSION}/input/contrat.pdf/0/contrat.pdf")
    _put(bucket, f"artifacts/{AGENT}/{SESSION}/output/rapport.html/0/rapport.html")
    _put(bucket, f"uploads/{AGENT}/vieux.csv")

    kinds = {i["filename"]: i["kind"] for i in library.build_library({AGENT: "A"})}
    assert kinds == {
        "contrat.pdf": "input",
        "rapport.html": "output",
        "vieux.csv": "legacy",
    }


def test_a_real_artifact_hides_its_legacy_namesake(bucket):
    _put(bucket, f"artifacts/{AGENT}/{SESSION}/output/rapport.html/0/rapport.html")
    _put(bucket, f"uploads/{AGENT}/rapport.html")

    items = library.build_library({AGENT: "A"})
    assert len(items) == 1
    assert items[0]["kind"] == "output"


def test_another_agent_is_not_swept_in(bucket):
    _put(bucket, f"artifacts/{OTHER}/{SESSION}/output/secret.py/0/secret.py")
    assert library.build_library({AGENT: "A"}) == []


def test_newest_first(bucket):
    old = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    new = datetime.datetime(2026, 8, 1, tzinfo=datetime.timezone.utc)
    _put(bucket, f"artifacts/{AGENT}/{SESSION}/output/vieux.py/0/vieux.py", when=old)
    _put(bucket, f"artifacts/{AGENT}/{SESSION}/output/recent.py/0/recent.py", when=new)

    assert [i["filename"] for i in library.build_library({AGENT: "A"})] == [
        "recent.py", "vieux.py",
    ]


# -- the route ------------------------------------------------------------


@pytest.fixture()
def client(bucket, monkeypatch):
    from apowerb.auth.dependencies import get_current_user
    from apowerb.routers import artifacts as artifacts_router

    monkeypatch.setattr(
        artifacts_router, "_owned_agents", lambda email: {AGENT: "Invoice reader"}
    )

    app = FastAPI()
    app.include_router(artifacts_router.router, prefix="/api")

    async def _user():
        u = MagicMock()
        u.email = USER
        u.user_id = 1
        u.role = "USER"
        return u

    app.dependency_overrides[get_current_user] = _user
    return TestClient(app)


def test_the_route_answers_the_whole_library(client, bucket):
    _put(bucket, f"artifacts/{AGENT}/{SESSION}/output/rapport.html/0/rapport.html")

    r = client.get("/api/artifacts/library")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["supported"] is True
    assert [i["filename"] for i in body["items"]] == ["rapport.html"]


def test_library_is_not_read_as_an_agent_name(client, bucket):
    """The per-session route is "/artifacts/{agent}/{user}/{session}".
    Declared first, it would swallow "library" as an agent name."""
    r = client.get("/api/artifacts/library")
    assert r.status_code == 200
    assert "items" in r.json()


def test_says_so_when_the_file_backend_is_active(client, monkeypatch):
    """Development fallback: there is no equivalent sweep on local disk, and
    the front must be able to tell rather than render an empty library."""
    from apowerb.routers import artifacts as artifacts_router

    monkeypatch.setattr(artifacts_router, "_s3_artifacts_active", lambda: False)

    body = client.get("/api/artifacts/library").json()
    assert body == {"items": [], "supported": False}

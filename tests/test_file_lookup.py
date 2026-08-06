"""Three storage conventions coexist, and each reader only knew one of them.

A document attached to a conversation is written as an input artifact under
its session (#30). `read_uploaded_file` looked under `uploads/{agent}/` only,
so the agent answered "file not found" one turn after a successful upload --
seen in production on 06/08 with a PDF. Generated files had the mirror
problem: `create_downloadable_file` wrote to `uploads/`, which the Artifacts
tab never reads.

These tests pin the resolution order and the fallbacks.
"""

from __future__ import annotations

import datetime
import json

import pytest

from apowerb.artifacts import file_lookup
from apowerb.storage import s3 as s3_storage
from tests.helpers.fake_s3 import FakeS3Client

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
def bucket(monkeypatch):
    fake = FakeS3Client()
    monkeypatch.setattr(s3_storage, "_get_s3_client", lambda: fake)

    settings = s3_storage.get_settings()
    monkeypatch.setattr(settings, "s3_bucket_name", "test-bucket", raising=False)
    monkeypatch.setattr(settings, "s3_access_key", "AK", raising=False)
    monkeypatch.setattr(settings, "s3_access_key_secret", "SK", raising=False)
    monkeypatch.setattr(settings, "s3_endpoint", "https://s3.example.com", raising=False)
    monkeypatch.setattr(settings, "s3_region", "gra", raising=False)
    return fake


def test_finds_a_document_attached_to_a_conversation(bucket):
    """The exact case that broke: uploaded, stored, then unreachable."""
    _put(bucket, f"artifacts/{AGENT}/{SESSION}/input/contrat.pdf/0/contrat.pdf", b"%PDF-1.4 x")
    assert file_lookup.read_file_bytes(AGENT, "contrat.pdf") == b"%PDF-1.4 x"


def test_finds_an_upload_made_outside_a_conversation(bucket):
    _put(bucket, f"artifacts/{AGENT}/_shared/input/brief.md/0/brief.md", b"# brief")
    assert file_lookup.read_file_bytes(AGENT, "brief.md") == b"# brief"


def test_finds_a_file_the_agent_generated(bucket):
    body = json.dumps({"filename": "r.html", "language": "html", "code": "<h1>x</h1>"}).encode()
    _put(bucket, f"artifacts/{AGENT}/{SESSION}/output/r.html/0/r.html", body)
    assert file_lookup.read_file_bytes(AGENT, "r.html") == body


def test_still_finds_the_455_legacy_files(bucket):
    """Nothing writes to uploads/ any more, but the objects are real data."""
    _put(bucket, f"uploads/{AGENT}/rapport_ia_sante_v2.html", b"<h1>old</h1>")
    assert file_lookup.read_file_bytes(AGENT, "rapport_ia_sante_v2.html") == b"<h1>old</h1>"


def test_latest_version_wins_numerically(bucket):
    for version, body in ((0, b"v0"), (2, b"v2"), (10, b"v10")):
        _put(bucket, f"artifacts/{AGENT}/{SESSION}/input/notes.txt/{version}/notes.txt", body)
    # "10" < "2" as strings: a lexical max would serve v2.
    assert file_lookup.read_file_bytes(AGENT, "notes.txt") == b"v10"


def test_an_upload_wins_over_a_generated_file_of_the_same_name(bucket):
    """`read_uploaded_file` is asked about a file the *user* sent."""
    _put(bucket, f"artifacts/{AGENT}/{SESSION}/input/report.html/0/report.html", b"uploaded")
    _put(bucket, f"artifacts/{AGENT}/{SESSION}/output/report.html/0/report.html", b"generated")
    assert file_lookup.read_file_bytes(AGENT, "report.html") == b"uploaded"


def test_metadata_siblings_are_not_mistaken_for_the_file(bucket):
    _put(bucket, f"artifacts/{AGENT}/{SESSION}/input/notes.txt/0/notes.txt", b"real")
    _put(bucket, f"artifacts/{AGENT}/{SESSION}/input/notes.txt/0/metadata.json", b"{}")
    assert file_lookup.read_file_bytes(AGENT, "notes.txt") == b"real"


def test_another_agent_is_not_reachable(bucket):
    _put(bucket, f"artifacts/other-agent/{SESSION}/input/secret.txt/0/secret.txt", b"nope")
    assert file_lookup.read_file_bytes(AGENT, "secret.txt") is None


def test_missing_file_reports_what_exists(bucket):
    _put(bucket, f"artifacts/{AGENT}/{SESSION}/input/contrat.pdf/0/contrat.pdf", b"x")
    _put(bucket, f"artifacts/{AGENT}/{SESSION}/output/rapport.html/0/rapport.html", b"y")
    _put(bucket, f"uploads/{AGENT}/vieux.csv", b"z")

    assert file_lookup.read_file_bytes(AGENT, "absent.txt") is None
    assert file_lookup.available_filenames(AGENT) == ["contrat.pdf", "rapport.html", "vieux.csv"]


def test_an_agent_reads_back_the_file_it_just_wrote_on_s3(bucket, monkeypatch):
    """The same round trip test_uploads_wiring.py pins for local disk, on the
    S3 path -- where writer and reader had drifted apart: one wrote output
    artifacts, the other looked under uploads/."""
    from apowerb.configs.settings import get_settings
    from apowerb.core.agent_helpers.read_file_tool import _make_read_uploaded_file
    from apowerb.core.agent_helpers.tool_factories import _make_create_downloadable_file

    monkeypatch.setattr(get_settings(), "storage_mode", "S3", raising=False)

    write = _make_create_downloadable_file(AGENT)
    read = _make_read_uploaded_file(AGENT)

    written = write("rapport.html", "<h1>bonjour</h1>")
    assert written["status"] == "success", written

    back = read("rapport.html")
    assert back["status"] == "success", back
    assert "bonjour" in back["content"]


def test_a_generated_file_lands_in_the_artifact_layout(bucket, monkeypatch):
    """Not under uploads/, which no reader of the Artifacts tab looks at."""
    from apowerb.configs.settings import get_settings
    from apowerb.core.agent_helpers.tool_factories import _make_create_downloadable_file

    monkeypatch.setattr(get_settings(), "storage_mode", "S3", raising=False)
    _make_create_downloadable_file(AGENT)("note.md", "texte")

    keys = list(bucket._objects)
    assert any(k.startswith(f"artifacts/{AGENT}/") and "/output/note.md/" in k for k in keys), keys
    assert not any(k.startswith(f"uploads/{AGENT}/") for k in keys), keys

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


_DAY_14 = datetime.datetime(2026, 10, 14, 9, 0, tzinfo=datetime.timezone.utc)
_DAY_16 = datetime.datetime(2026, 10, 16, 9, 0, tzinfo=datetime.timezone.utc)


def _put_at(fake: FakeS3Client, key: str, body: bytes, when: datetime.datetime) -> None:
    _put(fake, key, body)
    fake._objects[key]["last_modified"] = when


@pytest.mark.parametrize("old_scope, new_scope", [
    # Each session numbers its uploads from 0, so both copies are version 0;
    # the listing is sorted by key and the older session came first.
    ("session_1760432400000", "session_1760605200000"),
    # `_shared` sorts before any `session_…`: same failure the other way round.
    ("session_1760432400000", "_shared"),
    ("_shared", "session_1760605200000"),
])
def test_a_file_reattached_under_the_same_name_is_the_one_read(bucket, old_scope, new_scope):
    """Seen on dev 16/10: online_store_orders.csv attached again with new
    content (908 351 bytes) and the agent read the 14/10 copy (901 110)."""
    name = "online_store_orders.csv"
    _put_at(bucket, f"artifacts/{AGENT}/{old_scope}/input/{name}/0/{name}", b"14/10", _DAY_14)
    _put_at(bucket, f"artifacts/{AGENT}/{new_scope}/input/{name}/0/{name}", b"16/10", _DAY_16)
    assert file_lookup.read_file_bytes(AGENT, name) == b"16/10"


def test_a_newer_session_wins_even_with_a_lower_version(bucket):
    """Version numbers are per session: v3 of an old session is not newer
    than v0 of today's."""
    name = "orders.csv"
    _put_at(bucket, f"artifacts/{AGENT}/session_1760605200000/input/{name}/0/{name}", b"new", _DAY_16)
    _put_at(bucket, f"artifacts/{AGENT}/session_1760432400000/input/{name}/3/{name}", b"old", _DAY_14)
    assert file_lookup.read_file_bytes(AGENT, name) == b"new"


def test_read_uploaded_file_reports_the_reattached_file(bucket, monkeypatch):
    """The symptom as the agent saw it: the size of the previous upload."""
    from apowerb.configs.settings import get_settings
    from apowerb.core.agent_helpers.read_file_tool import _make_read_uploaded_file

    monkeypatch.setattr(get_settings(), "storage_mode", "S3", raising=False)
    name = "online_store_orders.csv"
    old, new = b"day,roas\n14,32.8175\n", b"day,roas\n16,41.0\n16,12.5\n"
    _put_at(bucket, f"artifacts/{AGENT}/session_1760432400000/input/{name}/0/{name}", old, _DAY_14)
    _put_at(bucket, f"artifacts/{AGENT}/session_1760605200000/input/{name}/0/{name}", new, _DAY_16)

    result = _make_read_uploaded_file(AGENT)(name)
    assert result["size"] == len(new), result


# -- one listing per read --------------------------------------------------
#
# Measured 16/10: a missing file cost two full scans of artifacts/{agent}/
# (read_file_bytes, then available_filenames for the error message) plus two
# legacy LISTs -- 4 calls for the 311 objects of the busiest dev agent, 42 at
# 20 000. The answer and the names come from the same listing.


def _paged_bucket(monkeypatch, page_size: int) -> FakeS3Client:
    fake = FakeS3Client(page_size=page_size)
    monkeypatch.setattr(s3_storage, "_get_s3_client", lambda: fake)
    monkeypatch.setattr(s3_storage.get_settings(), "s3_bucket_name", "test-bucket", raising=False)
    for i in range(311):
        name = f"f{i:03d}.csv"
        _put(fake, f"artifacts/{AGENT}/session_{i:05d}/input/{name}/0/{name}", b"x")
    return fake


def test_a_missing_file_lists_the_agent_prefix_once(monkeypatch):
    fake = _paged_bucket(monkeypatch, page_size=100)  # 311 objects = 4 pages
    _put(fake, f"uploads/{AGENT}/vieux.csv", b"z")

    content, names = file_lookup.read_file_and_names(AGENT, "absent.csv")

    assert content is None
    assert "f000.csv" in names and "vieux.csv" in names and len(names) == 312
    # 4 pages of artifacts/{agent}/ + 1 legacy LIST; was 2 x 4 + 2 = 10.
    assert fake.list_calls == 5


def test_a_found_file_costs_one_scan_and_no_legacy_list(monkeypatch):
    fake = _paged_bucket(monkeypatch, page_size=100)

    content, names = file_lookup.read_file_and_names(AGENT, "f007.csv")

    assert (content, names) == (b"x", [])
    assert fake.list_calls == 4


def test_a_legacy_file_is_read_without_listing_names(bucket):
    _put(bucket, f"uploads/{AGENT}/vieux.csv", b"z")
    _put(bucket, f"uploads/{AGENT}/vieux.csv.bak", b"bak")

    assert file_lookup.read_file_and_names(AGENT, "vieux.csv") == (b"z", [])
    # Exact membership, as file_exists_in_s3: a longer name does not answer.
    assert file_lookup.read_file_and_names(AGENT, "vieux")[0] is None
    assert bucket.list_calls == 4


def test_the_newest_upload_still_wins_through_the_single_listing(bucket):
    name = "online_store_orders.csv"
    _put_at(bucket, f"artifacts/{AGENT}/session_1760432400000/input/{name}/0/{name}", b"14/10", _DAY_14)
    _put_at(bucket, f"artifacts/{AGENT}/_shared/input/{name}/0/{name}", b"16/10", _DAY_16)
    assert file_lookup.read_file_and_names(AGENT, name) == (b"16/10", [])


def test_read_uploaded_file_lists_once_for_a_missing_file(monkeypatch):
    from apowerb.configs.settings import get_settings
    from apowerb.core.agent_helpers.read_file_tool import _make_read_uploaded_file

    monkeypatch.setattr(get_settings(), "storage_mode", "S3", raising=False)
    # S3 is used only when selected AND fully configured.
    for field in ("s3_bucket_name", "s3_access_key", "s3_access_key_secret",
                  "s3_endpoint", "s3_region"):
        monkeypatch.setattr(get_settings(), field, "x", raising=False)
    fake = _paged_bucket(monkeypatch, page_size=100)

    result = _make_read_uploaded_file(AGENT)("absent.csv")

    assert result["status"] == "error" and len(result["available_files"]) == 311
    assert fake.list_calls == 5

"""S3 listings must be complete, cheap, and exact.

`list_objects_v2` returns at most 1000 keys and reports the rest through a
continuation token. Reading only the first page truncates in silence: the
caller gets a short list, no error, and a file that exists is reported
missing — the same failure mode this module was written to remove. The
busiest agent prefix on the dev bucket already holds 311 objects.
"""

from __future__ import annotations

import datetime

import pytest

from apowerb.artifacts import file_lookup
from apowerb.storage import s3 as s3_storage
from tests.helpers.fake_s3 import FakeS3Client

AGENT = "agent1238"
SESSION = "session_1785927822560"


def _put(fake: FakeS3Client, key: str, body: bytes = b"x") -> None:
    fake._objects[key] = {
        "body": body,
        "content_type": "application/octet-stream",
        "metadata": {},
        "last_modified": datetime.datetime.now(datetime.timezone.utc),
    }


def _configure(monkeypatch, fake):
    monkeypatch.setattr(s3_storage, "_get_s3_client", lambda: fake)
    settings = s3_storage.get_settings()
    monkeypatch.setattr(settings, "s3_bucket_name", "test-bucket", raising=False)
    monkeypatch.setattr(settings, "s3_access_key", "AK", raising=False)
    monkeypatch.setattr(settings, "s3_access_key_secret", "SK", raising=False)
    monkeypatch.setattr(settings, "s3_endpoint", "https://s3.example.com", raising=False)
    monkeypatch.setattr(settings, "s3_region", "gra", raising=False)


@pytest.fixture()
def paged_bucket(monkeypatch):
    fake = FakeS3Client(page_size=2)
    _configure(monkeypatch, fake)
    return fake


@pytest.fixture()
def bucket(monkeypatch):
    fake = FakeS3Client()
    _configure(monkeypatch, fake)
    return fake


def test_listing_follows_continuation_tokens(paged_bucket):
    for i in range(7):
        _put(paged_bucket, f"uploads/{AGENT}/file{i}.txt")

    keys = s3_storage.list_files_in_s3(prefix=f"uploads/{AGENT}/")
    assert len(keys) == 7, keys


def test_a_file_on_a_later_page_is_still_found(paged_bucket):
    """The truncation bug in one line: the file exists, sits past the first
    page, and used to come back as missing."""
    for i in range(6):
        _put(paged_bucket, f"artifacts/{AGENT}/{SESSION}/input/a{i}.txt/0/a{i}.txt")
    # Sorts last, so it only shows up once the continuation is followed.
    _put(paged_bucket,
         f"artifacts/{AGENT}/{SESSION}/input/zzz.pdf/0/zzz.pdf", b"trouve")

    assert file_lookup.read_file_bytes(AGENT, "zzz.pdf") == b"trouve"


def test_one_listing_resolves_both_segments(bucket):
    """Asking per segment doubled the round trips for the same bytes."""
    _put(bucket, f"artifacts/{AGENT}/{SESSION}/output/rapport.html/0/rapport.html")

    bucket.list_calls = 0
    key = file_lookup.resolve_file_key(AGENT, "rapport.html")
    assert key is not None
    # An input answered on the first listing already; an output paid for two.
    assert bucket.list_calls == 1, f"{bucket.list_calls} listings"


def test_existence_does_not_match_a_longer_name(bucket):
    """The old check listed on the full key as a prefix, so any key merely
    starting with it answered true."""
    _put(bucket, f"uploads/{AGENT}/report.html.bak")

    assert s3_storage.file_exists_in_s3(f"uploads/{AGENT}/report.html") is False
    assert s3_storage.file_exists_in_s3(f"uploads/{AGENT}/report.html.bak") is True


def test_existence_costs_no_listing(bucket):
    _put(bucket, f"uploads/{AGENT}/brief.md")

    bucket.list_calls = 0
    assert s3_storage.file_exists_in_s3(f"uploads/{AGENT}/brief.md") is True
    assert bucket.list_calls == 0

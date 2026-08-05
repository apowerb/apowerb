"""``upload_bytes_to_s3`` gains an optional ``metadata`` kwarg, and two new
metadata-reading primitives (``get_object_with_metadata``,
``head_object_in_s3``) are added — needed by ``S3ArtifactService`` to store
and read ADK's ``custom_metadata`` and content-type without a separate
metadata.json (see the gcs_artifact_service.py it mirrors).

The addition to ``upload_bytes_to_s3`` must stay backward compatible: every
existing caller invokes it without ``metadata`` and must keep working.
"""

from __future__ import annotations

import pytest

from apowerb.storage import s3
from tests.helpers.fake_s3 import FakeS3Client


@pytest.fixture
def fake_client(monkeypatch):
    client = FakeS3Client()
    monkeypatch.setattr(s3, "_get_s3_client", lambda: client)
    monkeypatch.setattr(s3.settings, "s3_bucket_name", "test-bucket", raising=False)
    return client


class TestUploadBytesToS3Metadata:
    def test_existing_callers_without_metadata_still_work(self, fake_client):
        key = s3.upload_bytes_to_s3(b"hello", "some/key.txt")
        assert key == "some/key.txt"
        assert s3.download_file_from_s3("some/key.txt") == b"hello"

    def test_metadata_is_stored_alongside_content(self, fake_client):
        s3.upload_bytes_to_s3(
            b"payload", "some/key.txt", content_type="text/plain",
            metadata={"origin": "agent42"},
        )
        result = s3.get_object_with_metadata("some/key.txt")
        assert result["body"] == b"payload"
        assert result["content_type"] == "text/plain"
        assert result["metadata"] == {"origin": "agent42"}


class TestGetObjectWithMetadata:
    def test_missing_key_raises_client_error(self, fake_client):
        from botocore.exceptions import ClientError

        with pytest.raises(ClientError):
            s3.get_object_with_metadata("does/not/exist")


class TestHeadObjectInS3:
    def test_returns_none_when_key_missing(self, fake_client):
        assert s3.head_object_in_s3("does/not/exist") is None

    def test_returns_content_type_and_metadata_when_present(self, fake_client):
        s3.upload_bytes_to_s3(
            b"x", "k", content_type="application/json", metadata={"v": "3"},
        )
        head = s3.head_object_in_s3("k")
        assert head["content_type"] == "application/json"
        assert head["metadata"] == {"v": "3"}
        assert head["last_modified"] is not None

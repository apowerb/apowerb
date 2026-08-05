"""Unit tests for S3ArtifactService's input-artifact side (uploads).

Not part of ADK's artifact protocol (ADK only knows "output" -- what a tool
writes). Uploads are a product concept layered on the exact same key scheme
and S3 primitives as the existing "output" side, with "input" swapped in for
"output" via the `segment` parameter -- see S3ArtifactService docstring.

Key layout:

    artifacts/{app_name}/{session_id}/input/{filename}/{version}/{filename}

Uses the same FakeS3Client as tests/test_s3_artifact_service.py so these
tests exercise the real key-building and version logic without a network
call.
"""

from __future__ import annotations

import pytest

from apowerb.artifacts.s3_artifact_service import S3ArtifactService
from apowerb.storage import s3 as s3_storage
from tests.helpers.fake_s3 import FakeS3Client

APP_NAME = "agent1164"
SESSION_ID = "session_123"


@pytest.fixture
def fake_client(monkeypatch):
    client = FakeS3Client()
    monkeypatch.setattr(s3_storage, "_get_s3_client", lambda: client)
    monkeypatch.setattr(s3_storage.settings, "s3_bucket_name", "test-bucket", raising=False)
    return client


@pytest.fixture
def service(fake_client):
    return S3ArtifactService()


class TestSaveLoadRoundtrip:
    @pytest.mark.asyncio
    async def test_save_then_load_returns_same_bytes(self, service):
        version = await service.save_input_artifact(
            app_name=APP_NAME, session_id=SESSION_ID, filename="report.pdf",
            data=b"hello world", content_type="application/pdf",
        )
        assert version == 0

        loaded = await service.load_input_artifact(
            app_name=APP_NAME, session_id=SESSION_ID, filename="report.pdf",
        )
        assert loaded["data"] == b"hello world"
        assert loaded["content_type"] == "application/pdf"
        assert loaded["version"] == 0

    @pytest.mark.asyncio
    async def test_key_uses_input_segment_not_output(self, service, fake_client):
        await service.save_input_artifact(
            app_name=APP_NAME, session_id=SESSION_ID, filename="report.pdf",
            data=b"x", content_type="application/pdf",
        )
        expected_key = "artifacts/agent1164/session_123/input/report.pdf/0/report.pdf"
        assert expected_key in fake_client._objects
        assert "artifacts/agent1164/session_123/output/report.pdf/0/report.pdf" not in fake_client._objects

    @pytest.mark.asyncio
    async def test_load_missing_input_artifact_returns_none(self, service):
        result = await service.load_input_artifact(
            app_name=APP_NAME, session_id=SESSION_ID, filename="nope.pdf",
        )
        assert result is None


class TestVersioning:
    @pytest.mark.asyncio
    async def test_second_save_increments_version(self, service):
        v0 = await service.save_input_artifact(
            app_name=APP_NAME, session_id=SESSION_ID, filename="f.txt",
            data=b"v0", content_type="text/plain",
        )
        v1 = await service.save_input_artifact(
            app_name=APP_NAME, session_id=SESSION_ID, filename="f.txt",
            data=b"v1", content_type="text/plain",
        )
        assert (v0, v1) == (0, 1)

    @pytest.mark.asyncio
    async def test_load_without_version_returns_latest(self, service):
        await service.save_input_artifact(
            app_name=APP_NAME, session_id=SESSION_ID, filename="f.txt",
            data=b"v0", content_type="text/plain",
        )
        await service.save_input_artifact(
            app_name=APP_NAME, session_id=SESSION_ID, filename="f.txt",
            data=b"v1", content_type="text/plain",
        )
        latest = await service.load_input_artifact(
            app_name=APP_NAME, session_id=SESSION_ID, filename="f.txt",
        )
        assert latest["data"] == b"v1"


class TestListInputArtifactFilenames:
    @pytest.mark.asyncio
    async def test_lists_filenames_for_scope(self, service):
        await service.save_input_artifact(
            app_name=APP_NAME, session_id=SESSION_ID, filename="a.pdf",
            data=b"a", content_type="application/pdf",
        )
        await service.save_input_artifact(
            app_name=APP_NAME, session_id=SESSION_ID, filename="b.pdf",
            data=b"b", content_type="application/pdf",
        )
        names = await service.list_input_artifact_filenames(
            app_name=APP_NAME, session_id=SESSION_ID,
        )
        assert names == ["a.pdf", "b.pdf"]

    @pytest.mark.asyncio
    async def test_scopes_are_isolated(self, service):
        await service.save_input_artifact(
            app_name=APP_NAME, session_id="session_a", filename="a.pdf",
            data=b"a", content_type="application/pdf",
        )
        await service.save_input_artifact(
            app_name=APP_NAME, session_id="session_b", filename="b.pdf",
            data=b"b", content_type="application/pdf",
        )
        names = await service.list_input_artifact_filenames(
            app_name=APP_NAME, session_id="session_a",
        )
        assert names == ["a.pdf"]


class TestInputOutputIsolation:
    @pytest.mark.asyncio
    async def test_input_and_output_do_not_collide(self, service):
        """The existing output side (#28/#29) and the new input side share
        app_name/session_id but must never resolve to the same object."""
        from google.genai import types

        await service.save_artifact(
            app_name=APP_NAME, user_id="u1", filename="f.txt",
            artifact=types.Part(text="output-content"), session_id=SESSION_ID,
        )
        await service.save_input_artifact(
            app_name=APP_NAME, session_id=SESSION_ID, filename="f.txt",
            data=b"input-content", content_type="text/plain",
        )

        output_keys = await service.list_artifact_keys(
            app_name=APP_NAME, user_id="u1", session_id=SESSION_ID,
        )
        input_names = await service.list_input_artifact_filenames(
            app_name=APP_NAME, session_id=SESSION_ID,
        )
        assert output_keys == ["f.txt"]
        assert input_names == ["f.txt"]

        loaded_input = await service.load_input_artifact(
            app_name=APP_NAME, session_id=SESSION_ID, filename="f.txt",
        )
        assert loaded_input["data"] == b"input-content"

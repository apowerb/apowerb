"""Unit tests for S3ArtifactService, ADK's BaseArtifactService backed by
S3-compatible object storage (no built-in ADK S3 backend exists — only
file://, gs:// and in-memory).

Key layout imposed by product (see S3ArtifactService docstring):

    artifacts/{app_name}/{session_id}/output/{filename}/{version}/{filename}
    artifacts/{app_name}/user/{user_id}/output/{filename}/{version}/{filename}  (user-scoped)

All S3 I/O goes through a FakeS3Client (see tests/helpers/fake_s3.py) so
these tests exercise the real key-building and version logic without a
network call. The real bucket is exercised separately in
tests/test_s3_artifact_service_real_bucket.py.
"""

from __future__ import annotations

import pytest
from google.adk.errors.input_validation_error import InputValidationError
from google.genai import types

from apowerb.artifacts.s3_artifact_service import S3ArtifactService
from apowerb.storage import s3 as s3_storage
from tests.helpers.fake_s3 import FakeS3Client

APP_NAME = "agent42"
USER_ID = "user-1"
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


def _text_part(text: str) -> types.Part:
    return types.Part(text=text)


def _decoded(part: types.Part) -> str:
    """``Part.text`` is a plain field, never populated by ``from_bytes`` —
    loading always comes back as ``inline_data`` (this mirrors GCS's own
    ``_load_artifact``, verified against the real ``google.genai.types.Part``
    behavior, not an assumption)."""
    return part.inline_data.data.decode("utf-8")


class TestSaveLoadRoundtrip:
    @pytest.mark.asyncio
    async def test_save_then_load_returns_same_bytes(self, service):
        version = await service.save_artifact(
            app_name=APP_NAME, user_id=USER_ID, filename="report.txt",
            artifact=_text_part("hello world"), session_id=SESSION_ID,
        )
        assert version == 0

        loaded = await service.load_artifact(
            app_name=APP_NAME, user_id=USER_ID, filename="report.txt",
            session_id=SESSION_ID,
        )
        assert _decoded(loaded) == "hello world"

    @pytest.mark.asyncio
    async def test_key_matches_imposed_layout(self, service, fake_client):
        await service.save_artifact(
            app_name=APP_NAME, user_id=USER_ID, filename="report.txt",
            artifact=_text_part("x"), session_id=SESSION_ID,
        )
        expected_key = "artifacts/agent42/session_123/output/report.txt/0/report.txt"
        assert expected_key in fake_client._objects

    @pytest.mark.asyncio
    async def test_load_missing_artifact_returns_none(self, service):
        result = await service.load_artifact(
            app_name=APP_NAME, user_id=USER_ID, filename="nope.txt",
            session_id=SESSION_ID,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_session_scoped_without_session_id_raises(self, service):
        with pytest.raises(InputValidationError):
            await service.save_artifact(
                app_name=APP_NAME, user_id=USER_ID, filename="report.txt",
                artifact=_text_part("x"), session_id=None,
            )


class TestVersioning:
    @pytest.mark.asyncio
    async def test_second_save_increments_version(self, service):
        v0 = await service.save_artifact(
            app_name=APP_NAME, user_id=USER_ID, filename="f.txt",
            artifact=_text_part("v0"), session_id=SESSION_ID,
        )
        v1 = await service.save_artifact(
            app_name=APP_NAME, user_id=USER_ID, filename="f.txt",
            artifact=_text_part("v1"), session_id=SESSION_ID,
        )
        assert (v0, v1) == (0, 1)

    @pytest.mark.asyncio
    async def test_load_without_version_returns_latest(self, service):
        await service.save_artifact(
            app_name=APP_NAME, user_id=USER_ID, filename="f.txt",
            artifact=_text_part("v0"), session_id=SESSION_ID,
        )
        await service.save_artifact(
            app_name=APP_NAME, user_id=USER_ID, filename="f.txt",
            artifact=_text_part("v1"), session_id=SESSION_ID,
        )
        latest = await service.load_artifact(
            app_name=APP_NAME, user_id=USER_ID, filename="f.txt",
            session_id=SESSION_ID,
        )
        assert _decoded(latest) == "v1"

    @pytest.mark.asyncio
    async def test_load_explicit_old_version(self, service):
        await service.save_artifact(
            app_name=APP_NAME, user_id=USER_ID, filename="f.txt",
            artifact=_text_part("v0"), session_id=SESSION_ID,
        )
        await service.save_artifact(
            app_name=APP_NAME, user_id=USER_ID, filename="f.txt",
            artifact=_text_part("v1"), session_id=SESSION_ID,
        )
        first = await service.load_artifact(
            app_name=APP_NAME, user_id=USER_ID, filename="f.txt",
            session_id=SESSION_ID, version=0,
        )
        assert _decoded(first) == "v0"

    @pytest.mark.asyncio
    async def test_versions_sorted_numerically_not_lexically(self, service):
        # 11 saves -> versions 0..10. Lexical sort would put "10" before "2".
        for i in range(11):
            await service.save_artifact(
                app_name=APP_NAME, user_id=USER_ID, filename="f.txt",
                artifact=_text_part(f"v{i}"), session_id=SESSION_ID,
            )
        versions = await service.list_versions(
            app_name=APP_NAME, user_id=USER_ID, filename="f.txt",
            session_id=SESSION_ID,
        )
        assert versions == list(range(11))

        latest = await service.load_artifact(
            app_name=APP_NAME, user_id=USER_ID, filename="f.txt",
            session_id=SESSION_ID,
        )
        assert _decoded(latest) == "v10"


class TestListArtifactKeys:
    @pytest.mark.asyncio
    async def test_lists_session_scoped_filenames(self, service):
        await service.save_artifact(
            app_name=APP_NAME, user_id=USER_ID, filename="a.txt",
            artifact=_text_part("a"), session_id=SESSION_ID,
        )
        await service.save_artifact(
            app_name=APP_NAME, user_id=USER_ID, filename="b.txt",
            artifact=_text_part("b"), session_id=SESSION_ID,
        )
        keys = await service.list_artifact_keys(
            app_name=APP_NAME, user_id=USER_ID, session_id=SESSION_ID,
        )
        assert keys == ["a.txt", "b.txt"]

    @pytest.mark.asyncio
    async def test_includes_user_scoped_alongside_session_scoped(self, service):
        await service.save_artifact(
            app_name=APP_NAME, user_id=USER_ID, filename="a.txt",
            artifact=_text_part("a"), session_id=SESSION_ID,
        )
        await service.save_artifact(
            app_name=APP_NAME, user_id=USER_ID, filename="user:prefs.json",
            artifact=_text_part("{}"), session_id=None,
        )
        keys = await service.list_artifact_keys(
            app_name=APP_NAME, user_id=USER_ID, session_id=SESSION_ID,
        )
        assert keys == ["a.txt", "user:prefs.json"]

    @pytest.mark.asyncio
    async def test_none_session_id_returns_only_user_scoped(self, service):
        await service.save_artifact(
            app_name=APP_NAME, user_id=USER_ID, filename="a.txt",
            artifact=_text_part("a"), session_id=SESSION_ID,
        )
        await service.save_artifact(
            app_name=APP_NAME, user_id=USER_ID, filename="user:prefs.json",
            artifact=_text_part("{}"), session_id=None,
        )
        keys = await service.list_artifact_keys(
            app_name=APP_NAME, user_id=USER_ID, session_id=None,
        )
        assert keys == ["user:prefs.json"]


class TestDeleteArtifact:
    @pytest.mark.asyncio
    async def test_delete_removes_all_versions(self, service):
        await service.save_artifact(
            app_name=APP_NAME, user_id=USER_ID, filename="f.txt",
            artifact=_text_part("v0"), session_id=SESSION_ID,
        )
        await service.save_artifact(
            app_name=APP_NAME, user_id=USER_ID, filename="f.txt",
            artifact=_text_part("v1"), session_id=SESSION_ID,
        )
        await service.delete_artifact(
            app_name=APP_NAME, user_id=USER_ID, filename="f.txt",
            session_id=SESSION_ID,
        )
        remaining = await service.list_versions(
            app_name=APP_NAME, user_id=USER_ID, filename="f.txt",
            session_id=SESSION_ID,
        )
        assert remaining == []
        loaded = await service.load_artifact(
            app_name=APP_NAME, user_id=USER_ID, filename="f.txt",
            session_id=SESSION_ID,
        )
        assert loaded is None

    @pytest.mark.asyncio
    async def test_delete_does_not_touch_other_filenames(self, service):
        await service.save_artifact(
            app_name=APP_NAME, user_id=USER_ID, filename="keep.txt",
            artifact=_text_part("keep"), session_id=SESSION_ID,
        )
        await service.save_artifact(
            app_name=APP_NAME, user_id=USER_ID, filename="gone.txt",
            artifact=_text_part("gone"), session_id=SESSION_ID,
        )
        await service.delete_artifact(
            app_name=APP_NAME, user_id=USER_ID, filename="gone.txt",
            session_id=SESSION_ID,
        )
        kept = await service.load_artifact(
            app_name=APP_NAME, user_id=USER_ID, filename="keep.txt",
            session_id=SESSION_ID,
        )
        assert _decoded(kept) == "keep"


class TestUserScope:
    @pytest.mark.asyncio
    async def test_user_scoped_save_and_load_without_session(self, service):
        version = await service.save_artifact(
            app_name=APP_NAME, user_id=USER_ID, filename="user:settings.json",
            artifact=_text_part('{"theme":"dark"}'), session_id=None,
        )
        assert version == 0

        loaded = await service.load_artifact(
            app_name=APP_NAME, user_id=USER_ID, filename="user:settings.json",
            session_id=None,
        )
        assert _decoded(loaded) == '{"theme":"dark"}'

    @pytest.mark.asyncio
    async def test_user_scoped_key_has_no_session_segment(self, service, fake_client):
        await service.save_artifact(
            app_name=APP_NAME, user_id=USER_ID, filename="user:settings.json",
            artifact=_text_part("x"), session_id=None,
        )
        expected_key = (
            "artifacts/agent42/user/user-1/output/user:settings.json/0/user:settings.json"
        )
        assert expected_key in fake_client._objects

    @pytest.mark.asyncio
    async def test_different_users_do_not_collide(self, service):
        await service.save_artifact(
            app_name=APP_NAME, user_id="user-a", filename="user:settings.json",
            artifact=_text_part("a-settings"), session_id=None,
        )
        await service.save_artifact(
            app_name=APP_NAME, user_id="user-b", filename="user:settings.json",
            artifact=_text_part("b-settings"), session_id=None,
        )
        loaded_a = await service.load_artifact(
            app_name=APP_NAME, user_id="user-a", filename="user:settings.json",
            session_id=None,
        )
        assert _decoded(loaded_a) == "a-settings"


class TestArtifactVersionMetadata:
    @pytest.mark.asyncio
    async def test_get_artifact_version_returns_metadata(self, service):
        await service.save_artifact(
            app_name=APP_NAME, user_id=USER_ID, filename="f.txt",
            artifact=_text_part("hi"), session_id=SESSION_ID,
            custom_metadata={"source": "tool_save_code_artifact"},
        )
        av = await service.get_artifact_version(
            app_name=APP_NAME, user_id=USER_ID, filename="f.txt",
            session_id=SESSION_ID,
        )
        assert av.version == 0
        assert av.custom_metadata == {"source": "tool_save_code_artifact"}
        assert av.canonical_uri == (
            "s3://test-bucket/artifacts/agent42/session_123/output/f.txt/0/f.txt"
        )

    @pytest.mark.asyncio
    async def test_get_artifact_version_missing_returns_none(self, service):
        av = await service.get_artifact_version(
            app_name=APP_NAME, user_id=USER_ID, filename="nope.txt",
            session_id=SESSION_ID,
        )
        assert av is None

    @pytest.mark.asyncio
    async def test_list_artifact_versions_returns_all_in_order(self, service):
        for i in range(3):
            await service.save_artifact(
                app_name=APP_NAME, user_id=USER_ID, filename="f.txt",
                artifact=_text_part(f"v{i}"), session_id=SESSION_ID,
            )
        versions = await service.list_artifact_versions(
            app_name=APP_NAME, user_id=USER_ID, filename="f.txt",
            session_id=SESSION_ID,
        )
        assert [v.version for v in versions] == [0, 1, 2]

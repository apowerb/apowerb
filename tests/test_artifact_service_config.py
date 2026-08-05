"""Decides which artifact_service_uri ``main.py`` hands to ADK's
get_fast_api_app: S3 when fully configured, the existing file:// backend
otherwise — so a deployment without S3 credentials still boots instead of
crashing at startup.

Kept out of main.py itself and unit-tested in isolation: importing
apowerb.main constructs the whole FastAPI app at module level (existing
behavior, not something this change should make worse to test).
"""

from __future__ import annotations

from apowerb.configs.artifact_service_config import (
    is_s3_artifact_storage_configured,
    resolve_artifact_service_uri,
)
from apowerb.configs.settings import Settings


def _settings(**overrides) -> Settings:
    base = dict(
        storage_mode="S3",
        s3_bucket_name="th2agent-dev",
        s3_access_key="AK",
        s3_access_key_secret="SK",
        s3_endpoint="https://s3.example.com",
        s3_region="gra",
    )
    base.update(overrides)
    return Settings(**base)


class TestIsS3ArtifactStorageConfigured:
    def test_true_when_storage_mode_s3_and_all_fields_set(self):
        assert is_s3_artifact_storage_configured(_settings()) is True

    def test_false_when_storage_mode_is_local(self):
        assert is_s3_artifact_storage_configured(_settings(storage_mode="local")) is False

    def test_false_when_bucket_name_missing(self):
        assert is_s3_artifact_storage_configured(_settings(s3_bucket_name="")) is False

    def test_false_when_access_key_missing(self):
        assert is_s3_artifact_storage_configured(_settings(s3_access_key="")) is False

    def test_false_when_secret_missing(self):
        assert is_s3_artifact_storage_configured(_settings(s3_access_key_secret="")) is False

    def test_false_when_endpoint_missing(self):
        assert is_s3_artifact_storage_configured(_settings(s3_endpoint="")) is False

    def test_false_when_region_missing(self):
        assert is_s3_artifact_storage_configured(_settings(s3_region="")) is False


class TestResolveArtifactServiceUri:
    def test_returns_s3_uri_when_configured(self):
        uri = resolve_artifact_service_uri(_settings(), artifacts_dir="/var/artifacts")
        assert uri == "s3://th2agent-dev"

    def test_falls_back_to_file_uri_when_not_configured(self):
        # Matches the pre-existing formula in main.py verbatim: "file:///" +
        # os.path.abspath(...) on an already-absolute path yields 4 slashes,
        # not 3. Not this change's concern to normalize — preserving exact
        # prior behavior for the fallback path.
        uri = resolve_artifact_service_uri(
            _settings(storage_mode="local"), artifacts_dir="/var/artifacts"
        )
        assert uri == "file:////var/artifacts"

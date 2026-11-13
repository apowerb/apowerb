"""Files stay on disk when S3 is selected but not configured.

``storage_mode`` defaults to ``"S3"``. The artifact store and the ``/files``
download route fall back to local disk unless every S3 credential is set
(``is_s3_artifact_storage_configured``), but the agent file tools tested
``storage_mode == "local"`` and called S3 with an empty endpoint:
``create_downloadable_file`` failed with ``Invalid endpoint: ''`` on an
install without S3.
"""

from __future__ import annotations

import pytest

from apowerb.configs.paths import uploads_dir
from apowerb.configs.settings import get_settings
from apowerb.core.agent_helpers.read_file_tool import _make_read_uploaded_file
from apowerb.core.agent_helpers.tool_factories import _make_create_downloadable_file
from apowerb.storage.storage_service import StorageService

S3_FIELDS = ("s3_bucket_name", "s3_access_key", "s3_access_key_secret", "s3_endpoint", "s3_region")


@pytest.fixture
def s3_selected_without_credentials(monkeypatch, tmp_path):
    settings = get_settings()
    monkeypatch.setattr(settings, "runtime_root", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "storage_mode", "S3", raising=False)
    for field in S3_FIELDS:
        monkeypatch.setattr(settings, field, "", raising=False)
    return tmp_path


def test_downloadable_file_is_written_to_disk(s3_selected_without_credentials):
    result = _make_create_downloadable_file("agent7")("data.csv", "day,n\n2026-10-01,3\n")

    assert result["status"] == "success", result
    assert result["download_path"] == "/api/files/agent7/data.csv"
    assert (uploads_dir() / "agent7" / "data.csv").read_text() == "day,n\n2026-10-01,3\n"


def test_downloadable_file_reads_back(s3_selected_without_credentials):
    _make_create_downloadable_file("agent7")("note.md", "# Bilan")

    read = _make_read_uploaded_file("agent7")("note.md")

    assert "# Bilan" in str(read)


def test_storage_service_uses_disk_without_credentials(s3_selected_without_credentials):
    assert StorageService()._use_s3 is False


def test_storage_service_uses_s3_when_fully_configured(s3_selected_without_credentials, monkeypatch):
    for field in S3_FIELDS:
        monkeypatch.setattr(get_settings(), field, "x", raising=False)
    assert StorageService()._use_s3 is True

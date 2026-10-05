"""StorageService exposes one uniform interface over S3 and local disk (#140).

Before #140 the two modes bound different method names, so callers probed with
getattr. These tests pin the contract: the same five methods exist and behave
in both modes — local round-trips on disk, S3 delegates to apowerb.storage.s3.
"""

from __future__ import annotations

import pytest

from apowerb.configs.settings import get_settings
from apowerb.storage import s3 as s3_module
from apowerb.storage.storage_service import StorageService

_METHODS = (
    "upload_bytes_to_storage",
    "upload_file_to_storage",
    "download_file_from_storage",
    "file_exists_in_storage",
    "list_files_in_storage",
)


@pytest.fixture
def local_root(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "runtime_root", str(tmp_path), raising=False)
    monkeypatch.setattr(get_settings(), "storage_mode", "local", raising=False)
    return tmp_path


# ── Uniform interface in both modes ───────────────────────────────────────
@pytest.mark.parametrize("mode", ["S3", "local"])
def test_same_methods_in_both_modes(monkeypatch, mode):
    monkeypatch.setattr(get_settings(), "storage_mode", mode, raising=False)
    service = StorageService()
    for name in _METHODS:
        assert callable(getattr(service, name)), f"{name} missing in {mode} mode"


# ── Local backend: real round-trips ───────────────────────────────────────
class TestLocal:
    def test_upload_bytes_then_download(self, local_root):
        service = StorageService()
        returned = service.upload_bytes_to_storage(b"hello", "agent7/file.txt")
        assert returned == "agent7/file.txt"
        assert (local_root / "uploads" / "agent7" / "file.txt").read_bytes() == b"hello"
        assert service.download_file_from_storage("agent7/file.txt") == b"hello"

    def test_file_exists(self, local_root):
        service = StorageService()
        assert service.file_exists_in_storage("x/y.txt") is False
        service.upload_bytes_to_storage(b"1", "x/y.txt")
        assert service.file_exists_in_storage("x/y.txt") is True

    def test_list_files(self, local_root):
        service = StorageService()
        service.upload_bytes_to_storage(b"1", "p/a.txt")
        service.upload_bytes_to_storage(b"2", "p/sub/b.txt")
        listed = set(service.list_files_in_storage("p"))
        assert listed == {"p/a.txt", "p/sub/b.txt"}

    def test_list_missing_prefix_is_empty(self, local_root):
        assert StorageService().list_files_in_storage("nope") == []

    def test_upload_file_reads_from_disk(self, local_root, tmp_path):
        src = tmp_path / "src.bin"
        src.write_bytes(b"payload")
        service = StorageService()
        service.upload_file_to_storage(str(src), "dest/out.bin")
        assert service.download_file_from_storage("dest/out.bin") == b"payload"

    def test_download_missing_raises(self, local_root):
        with pytest.raises(FileNotFoundError):
            StorageService().download_file_from_storage("ghost.txt")


# ── S3 backend: delegates to apowerb.storage.s3 ───────────────────────────
class TestS3Delegation:
    @pytest.fixture(autouse=True)
    def _s3_mode(self, monkeypatch):
        monkeypatch.setattr(get_settings(), "storage_mode", "S3", raising=False)

    def test_upload_bytes_delegates(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            s3_module,
            "upload_bytes_to_s3",
            lambda content, key, ct: seen.update(content=content, key=key, ct=ct)
            or key,
        )
        out = StorageService().upload_bytes_to_storage(b"d", "k/f.txt", "text/plain")
        assert out == "k/f.txt"
        assert seen == {"content": b"d", "key": "k/f.txt", "ct": "text/plain"}

    def test_download_delegates(self, monkeypatch):
        monkeypatch.setattr(
            s3_module, "download_file_from_s3", lambda key: b"from-s3:" + key.encode()
        )
        assert StorageService().download_file_from_storage("k.txt") == b"from-s3:k.txt"

    def test_exists_delegates(self, monkeypatch):
        monkeypatch.setattr(s3_module, "file_exists_in_s3", lambda key: key == "there")
        service = StorageService()
        assert service.file_exists_in_storage("there") is True
        assert service.file_exists_in_storage("absent") is False

    def test_list_delegates(self, monkeypatch):
        monkeypatch.setattr(
            s3_module, "list_files_in_s3", lambda prefix: [prefix + "/a"]
        )
        assert StorageService().list_files_in_storage("p") == ["p/a"]

    def test_upload_file_delegates(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            s3_module,
            "upload_file_to_s3",
            lambda fp, key: seen.update(fp=fp, key=key) or f"s3://b/{key}",
        )
        out = StorageService().upload_file_to_storage("/tmp/x", "k/out")
        assert out == "s3://b/k/out"
        assert seen == {"fp": "/tmp/x", "key": "k/out"}

"""Uniform persistent-storage interface over blob (S3) or the local disk.

``StorageService`` presents the **same method names whatever the backend**, so a
caller never branches on ``storage_mode`` nor probes the instance with
``getattr`` to find out which upload/download it got. ``storage_mode == "S3"``
delegates to :mod:`apowerb.storage.s3` (the OVH object store); any other value
("local") reads and writes under :func:`apowerb.configs.paths.uploads_dir`.

Paths are backend-neutral keys — relative, no leading slash, e.g.
``"bug-reports/<fingerprint>/<id>.png"`` or ``"agent7/file.txt"``. In S3 mode a
key maps to an object key; in local mode it maps to a path under the uploads
directory.

History: before #140 the two modes bound *different* method names
(``upload_bytes_to_s3`` vs ``upload_bytes_to_storage``), so every caller had to
try both with ``getattr``. The generic ``*_storage`` names are now the single
interface (they were already what the local mode and the wiring tests used);
the S3-suffixed module functions still live in :mod:`apowerb.storage.s3` for
callers that talk to S3 directly.
"""

from __future__ import annotations

import os

from apowerb.configs.paths import uploads_dir
from apowerb.configs.settings import get_settings


def _local_full_path(path: str) -> str:
    """Resolve a backend-neutral key to an absolute path under uploads_dir."""
    return str(uploads_dir() / path)


def _upload_bytes_to_local(
    content: bytes, path: str, content_type: str = "application/octet-stream"
) -> str:
    """Write *content* under the uploads dir; returns the key, like the S3 path.

    ``content_type`` is accepted for interface parity and ignored on disk."""
    full_path = _local_full_path(path)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "wb") as f:
        f.write(content)
    return path


def _download_file_from_local(path: str) -> bytes:
    """Read a local file as bytes. Raises FileNotFoundError when absent."""
    with open(_local_full_path(path), "rb") as f:
        return f.read()


def _file_exists_in_local(path: str) -> bool:
    return os.path.exists(_local_full_path(path))


def _list_files_in_local(prefix: str = "") -> list[str]:
    """Keys under *prefix*, relative to the uploads dir (mirrors S3 listing)."""
    base = str(uploads_dir())
    full_path = _local_full_path(prefix)
    if not os.path.exists(full_path):
        return []
    files: list[str] = []
    for root, _dirs, filenames in os.walk(full_path):
        for filename in filenames:
            files.append(os.path.relpath(os.path.join(root, filename), base))
    return files


class StorageService:
    """Read/write bytes to persistent storage; backend chosen by ``storage_mode``.

    The same five methods exist in both modes:
    ``upload_bytes_to_storage``, ``upload_file_to_storage``,
    ``download_file_from_storage``, ``file_exists_in_storage`` and
    ``list_files_in_storage``.
    """

    def __init__(self) -> None:
        self.storage_mode = get_settings().storage_mode
        self._use_s3 = self.storage_mode == "S3"

    # ── writes ────────────────────────────────────────────────────────────
    def upload_bytes_to_storage(
        self,
        content: bytes,
        path: str,
        content_type: str = "application/octet-stream",
    ) -> str:
        """Store in-memory bytes at *path*; returns the stored key/path."""
        if self._use_s3:
            from .s3 import upload_bytes_to_s3

            return upload_bytes_to_s3(content, path, content_type)
        return _upload_bytes_to_local(content, path, content_type)

    def upload_file_to_storage(self, file_path: str, path: str) -> str:
        """Store a local file's contents at *path*; returns the stored key/path."""
        if self._use_s3:
            from .s3 import upload_file_to_s3

            return upload_file_to_s3(file_path, path)
        with open(file_path, "rb") as f:
            return _upload_bytes_to_local(f.read(), path)

    # ── reads ─────────────────────────────────────────────────────────────
    def download_file_from_storage(self, path: str) -> bytes:
        """Return the bytes stored at *path*. Raises when the object is absent."""
        if self._use_s3:
            from .s3 import download_file_from_s3

            return download_file_from_s3(path)
        return _download_file_from_local(path)

    def file_exists_in_storage(self, path: str) -> bool:
        if self._use_s3:
            from .s3 import file_exists_in_s3

            return file_exists_in_s3(path)
        return _file_exists_in_local(path)

    def list_files_in_storage(self, prefix: str = "") -> list[str]:
        if self._use_s3:
            from .s3 import list_files_in_s3

            return list_files_in_s3(prefix)
        return _list_files_in_local(prefix)

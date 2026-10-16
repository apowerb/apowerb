"""Reads a file an agent was given by name, wherever the platform stored it.

Agents only ever see a bare filename (``[Uploaded files: cv.pdf]``, or the
name ``create_downloadable_file`` returned). Where its bytes live depends on
the storage the upload route chose: S3 artifacts when S3 is configured
(``artifacts/{agent}/{session}/input|output/...``, resolved by
``file_lookup``), otherwise ``uploads/{agent}/`` on disk.

Tools that looked only on disk reported "not found" for every file in S3
mode. This module is the one place that knows both, so a tool never has to.
"""

from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional


def is_s3_storage() -> bool:
    """Same predicate the upload route uses to decide where files are written."""
    from apowerb.configs.artifact_service_config import (
        is_s3_artifact_storage_configured,
    )
    from apowerb.configs.settings import get_settings

    return is_s3_artifact_storage_configured(get_settings())


def is_bare_filename(name: str) -> bool:
    """True for a plain file name: no directory part, so it cannot escape."""
    return (
        name not in {"", ".", ".."}
        and name == os.path.basename(name)
        and "\\" not in name
    )


def read_agent_upload(folder: str, name: str) -> tuple[Optional[bytes], list[str]]:
    """``(bytes, [])`` for ``name`` in the agent ``folder`` (``agent{id}``),
    or ``(None, available_names)`` when it is not there or not a bare name."""
    from apowerb.configs import paths

    if not is_bare_filename(name):
        return None, []

    if is_s3_storage():
        from apowerb.artifacts.file_lookup import read_file_and_names

        return read_file_and_names(folder, name)

    base = (paths.uploads_dir() / folder).resolve()
    path = (base / name).resolve()
    if path.parent == base and path.is_file():
        return path.read_bytes(), []
    available = (
        sorted(p.name for p in base.iterdir() if p.is_file()) if base.is_dir() else []
    )
    return None, available


@contextmanager
def agent_upload_path(
    folder: str, name: str
) -> Iterator[tuple[Optional[str], list[str]]]:
    """Like :func:`read_agent_upload`, for code that needs a path on disk.

    Yields ``(path, [])`` or ``(None, available_names)``. On disk the real
    path is yielded; from S3 the bytes are written to a temporary file that
    keeps the original name (callers derive names from it) and is removed on
    exit.
    """
    from apowerb.configs import paths

    if not is_s3_storage():
        if is_bare_filename(name):
            base = (paths.uploads_dir() / folder).resolve()
            path = (base / name).resolve()
            if path.parent == base and path.is_file():
                yield str(path), []
                return
        _content, available = read_agent_upload(folder, name)
        yield None, available
        return

    content, available = read_agent_upload(folder, name)
    if content is None:
        yield None, available
        return
    with tempfile.TemporaryDirectory(prefix="agent-upload-") as tmp:
        path = Path(tmp) / name
        path.write_bytes(content)
        yield str(path), []

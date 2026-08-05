"""The code handed to Docker must live where the daemon can actually see it.

On both thaink2 VMs Docker is installed from snap, which confines ``/tmp``:
a bind mount of a host ``/tmp`` directory shows up **empty** inside the
container. ``execute_artifact`` wrote the file with
``tempfile.TemporaryDirectory()`` — i.e. under ``/tmp`` — so every run failed
with::

    python: can't open file '/tmp/code/fizzbuzz.py': [Errno 2] No such file

Measured on the dev host on 2026-08-04: mounting from ``/tmp`` gave an empty
directory, mounting the same file from ``/home/ubuntu`` ran it and printed its
output. The workspace therefore has to sit under the runtime root, which is
already outside ``/tmp`` on every deployment.
"""
from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, patch

import pytest

from apowerb.configs import paths


class _FakeProcess:
    returncode = 0

    async def communicate(self, input=None):
        return b"ok", b""


def _capture_docker_cmd(monkeypatch, tmp_path):
    """Run execute_artifact with Docker stubbed, return the argv it built."""
    monkeypatch.setattr(
        paths.get_settings(), "runtime_root", str(tmp_path), raising=False
    )
    seen = {}

    async def _fake_exec(*argv, **kwargs):
        seen["argv"] = argv
        return _FakeProcess()

    from apowerb.core import artifact_executor

    with patch.object(asyncio, "create_subprocess_exec", new=AsyncMock(side_effect=_fake_exec)):
        asyncio.run(
            artifact_executor.execute_artifact(
                code="print(1)", language="python", filename="a.py", timeout=5
            )
        )
    return seen["argv"]


def _mounted_host_dir(argv) -> str:
    volume = argv[argv.index("-v") + 1]
    return volume.split(":", 1)[0]


def test_the_workspace_is_not_the_system_temp_dir(monkeypatch, tmp_path):
    """The mount must not land straight in the system temp directory.

    Asserting ``not startswith("/tmp/")`` would be wrong here: pytest roots
    ``tmp_path`` under ``/tmp`` itself, so the check has to be that the
    workspace's parent is not the system temp dir — which is exactly what
    ``TemporaryDirectory()`` without ``dir=`` would produce.
    """
    import tempfile as _tempfile

    argv = _capture_docker_cmd(monkeypatch, tmp_path)
    host_dir = _mounted_host_dir(argv)

    system_tmp = os.path.realpath(_tempfile.gettempdir())
    assert os.path.realpath(os.path.dirname(host_dir)) != system_tmp, (
        "snap-installed Docker cannot see host /tmp — the mount would be empty"
    )


def test_the_workspace_lives_under_the_runtime_root(monkeypatch, tmp_path):
    argv = _capture_docker_cmd(monkeypatch, tmp_path)
    host_dir = _mounted_host_dir(argv)

    assert str(tmp_path) in host_dir


def test_the_workspace_follows_a_moved_runtime_root(monkeypatch, tmp_path):
    first = _mounted_host_dir(_capture_docker_cmd(monkeypatch, tmp_path / "a"))
    second = _mounted_host_dir(_capture_docker_cmd(monkeypatch, tmp_path / "b"))

    assert first != second, "the workspace is pinned instead of following the root"


def test_the_workspace_is_removed_after_the_run(monkeypatch, tmp_path):
    host_dir = _mounted_host_dir(_capture_docker_cmd(monkeypatch, tmp_path))

    assert not os.path.exists(host_dir), "the workspace outlived the execution"


def test_an_unsupported_language_never_reaches_docker(monkeypatch, tmp_path):
    from apowerb.core import artifact_executor

    result = asyncio.run(
        artifact_executor.execute_artifact(
            code="x", language="brainfuck", filename="a.bf", timeout=5
        )
    )
    assert result["exit_code"] != 0
    assert "Unsupported language" in result["stderr"]

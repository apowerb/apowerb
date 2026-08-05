"""
Regression tests for .github/scripts/wait-for-pypi-release.sh.

Context: the "Build and publish Docker image" workflow installs the just
published package straight from PyPI (`uv pip install apowerb==$VERSION`).
It is triggered by `workflow_run` on "Upload Python Package" completing, but
PyPI's index takes some time to propagate a freshly published release. This
is a race: releases 0.1.11 and 0.1.12 both failed the Docker build with
"Because there is no version of apowerb==X and you require apowerb==X, we
can conclude that your requirements are unsatisfiable", and both succeeded
on manual re-run once PyPI had caught up (proof: GitHub Actions runs
30983073450 attempt 1 and 30993097708 attempt 1 in apowerb/apowerb).

These tests exercise the polling script against a local HTTP server that
mimics PyPI's per-version JSON endpoint (200 once the version exists, 404
otherwise), so the retry/timeout logic is proven without depending on real
PyPI propagation timing.
"""

from __future__ import annotations

import http.server
import os
import subprocess
import threading
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "wait-for-pypi-release.sh"


class _FlakyPyPIHandler(http.server.BaseHTTPRequestHandler):
    """Serves 404 for the first N requests to a version, then 200."""

    ready_after_requests: int = 0
    request_count: int = 0

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        type(self).request_count += 1
        if type(self).request_count >= type(self).ready_after_requests:
            body = b'{"info": {"version": "0.1.12"}}'
            self.send_response(200)
        else:
            body = b"Not Found"
            self.send_response(404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:  # silence test output
        pass


class _AlwaysMissingHandler(http.server.BaseHTTPRequestHandler):
    request_count: int = 0

    def do_GET(self) -> None:  # noqa: N802
        type(self).request_count += 1
        body = b"Not Found"
        self.send_response(404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def mock_server():
    servers: list[http.server.HTTPServer] = []

    def _start(handler_cls: type[http.server.BaseHTTPRequestHandler]) -> str:
        handler_cls.request_count = 0
        server = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
        servers.append(server)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        return f"http://127.0.0.1:{port}"

    yield _start

    for server in servers:
        server.shutdown()
        server.server_close()


def _run_script(base_url: str, **env_overrides: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "PACKAGE_NAME": "apowerb",
            "PACKAGE_VERSION": "0.1.12",
            "PYPI_BASE_URL": base_url,
            "MAX_ATTEMPTS": "5",
            "SLEEP_SECONDS": "0",
        }
    )
    env.update(env_overrides)
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_script_exists_and_is_executable() -> None:
    assert SCRIPT.exists(), f"missing {SCRIPT}"
    assert os.access(SCRIPT, os.X_OK), f"{SCRIPT} must be executable"


def test_succeeds_immediately_when_version_already_available(mock_server) -> None:
    base_url = mock_server(_FlakyPyPIHandler)
    _FlakyPyPIHandler.ready_after_requests = 1

    result = _run_script(base_url)

    assert result.returncode == 0, result.stderr
    assert _FlakyPyPIHandler.request_count == 1
    assert "available on PyPI" in result.stdout


def test_retries_until_version_appears_within_budget(mock_server) -> None:
    base_url = mock_server(_FlakyPyPIHandler)
    _FlakyPyPIHandler.ready_after_requests = 3

    result = _run_script(base_url)

    assert result.returncode == 0, result.stderr
    assert _FlakyPyPIHandler.request_count == 3


def test_fails_explicitly_when_version_never_appears(mock_server) -> None:
    base_url = mock_server(_AlwaysMissingHandler)

    result = _run_script(base_url)

    assert result.returncode != 0
    assert _AlwaysMissingHandler.request_count == 5
    assert "never became available" in (result.stdout + result.stderr)


def test_never_sleeps_past_the_last_attempt(mock_server) -> None:
    """The script must not sleep after the final failing attempt (no wasted time)."""
    import time

    base_url = mock_server(_AlwaysMissingHandler)

    started = time.monotonic()
    result = _run_script(base_url, MAX_ATTEMPTS="2", SLEEP_SECONDS="5")
    elapsed = time.monotonic() - started

    assert result.returncode != 0
    # One sleep between attempt 1 and 2 (~5s) is expected; a second sleep
    # after the final failed attempt would push this past 10s.
    assert elapsed < 10, f"elapsed={elapsed:.1f}s suggests an extra sleep after the last attempt"

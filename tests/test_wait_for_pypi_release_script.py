"""
Regression tests for .github/scripts/wait-for-pypi-release.sh.

Context: the "Build and publish Docker image" workflow installs the just
published package straight from PyPI, in the Dockerfile, via
`uv pip install --system --no-cache-dir "apowerb==$VERSION"`. It is
triggered by `workflow_run` on "Upload Python Package" completing, but
PyPI's index takes some time to propagate a freshly published release.
This is a race: releases 0.1.11 and 0.1.12 both failed the Docker build
with "Because there is no version of apowerb==X and you require
apowerb==X, we can conclude that your requirements are unsatisfiable",
and both succeeded on manual re-run once PyPI had caught up (proof:
GitHub Actions runs 30983073450 attempt 1 and 30993097708 attempt 1 in
apowerb/apowerb).

An earlier version of this script polled PyPI's per-version JSON endpoint
(`/pypi/<name>/<version>/json`). That endpoint is a different Warehouse
code path from the Simple index the resolver actually reads
(https://pypi.org/simple/<name>/), served with its own caching, so it can
report "available" before `uv`'s resolver agrees - which would make the
wait step pass while the real Docker build still fails. The script now
runs `uv pip install --dry-run --system` directly: the same class of
command as the Dockerfile, resolved through the same code path, so it
fails and succeeds under exactly the condition the build depends on.

Two kinds of tests below:

- "Resolves the real condition" tests run the real `uv` binary against a
  wheel built with `uv build` and served locally via `--no-index
  --find-links`, so they prove the script's `uv` invocation is the actual
  faithful check (and would catch a regression back to a JSON/HTTP-status
  check, since that would never produce uv's resolver error text).
- "Retry policy" tests run against a fake `uv` stand-in so the bounded
  retry/backoff logic itself is proven deterministically, without relying
  on real timing.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "wait-for-pypi-release.sh"


def _run_script(env_overrides: dict[str, str], path_prefix: str | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.setdefault("MAX_ATTEMPTS", "5")
    env.setdefault("SLEEP_SECONDS", "0")
    env.update(env_overrides)
    if path_prefix:
        env["PATH"] = f"{path_prefix}:{env['PATH']}"
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_script_exists_and_is_executable() -> None:
    assert SCRIPT.exists(), f"missing {SCRIPT}"
    assert os.access(SCRIPT, os.X_OK), f"{SCRIPT} must be executable"


# --- Faithfulness to the real build: real `uv`, no network -----------------


@pytest.fixture(scope="module")
def fake_wheel_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A local package source containing exactly one real wheel, built by
    the real `uv build` (no fabricated/hand-crafted wheel), so resolving
    against it exercises uv's genuine resolver logic."""
    src = tmp_path_factory.mktemp("fakepkg-src")
    (src / "fakepkg").mkdir()
    (src / "fakepkg" / "__init__.py").write_text("")
    (src / "pyproject.toml").write_text(
        textwrap.dedent(
            """\
            [project]
            name = "fakepkg"
            version = "1.2.3"
            requires-python = ">=3.9"

            [build-system]
            requires = ["hatchling"]
            build-backend = "hatchling.build"
            """
        )
    )
    dist = src / "dist"
    result = subprocess.run(
        ["uv", "build", "-o", str(dist)],
        cwd=src,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert (dist / "fakepkg-1.2.3-py3-none-any.whl").exists()
    return dist


@pytest.fixture
def empty_package_dir(tmp_path: Path) -> Path:
    empty = tmp_path / "empty-index"
    empty.mkdir()
    return empty


def test_succeeds_when_uv_actually_resolves_the_package(fake_wheel_dir: Path) -> None:
    result = _run_script(
        {
            "PACKAGE_NAME": "fakepkg",
            "PACKAGE_VERSION": "1.2.3",
            "EXTRA_UV_ARGS": f"--no-index --find-links {fake_wheel_dir}",
        }
    )

    assert result.returncode == 0, result.stderr
    assert "resolves" in result.stdout


def test_fails_with_uvs_own_unsatisfiable_error_when_resolution_fails(empty_package_dir: Path) -> None:
    """This is the regression guard: a check that merely probes an HTTP
    status code (like the old JSON-endpoint version) can never produce this
    exact resolver error. If this assertion breaks, the script has stopped
    testing the real condition the Docker build depends on."""
    result = _run_script(
        {
            "PACKAGE_NAME": "fakepkg",
            "PACKAGE_VERSION": "1.2.3",
            "EXTRA_UV_ARGS": f"--no-index --find-links {empty_package_dir}",
            "MAX_ATTEMPTS": "1",
        }
    )

    assert result.returncode != 0
    assert "unsatisfiable" in (result.stdout + result.stderr)
    assert "fakepkg==1.2.3" in (result.stdout + result.stderr)


def test_invokes_uv_pip_install_with_dry_run_and_system_flags(fake_wheel_dir: Path, tmp_path: Path) -> None:
    """Guards the exact flags: --dry-run (no side effects), --system (same
    installation mode as the Dockerfile)."""
    argv_log = tmp_path / "argv.log"
    spy_dir = tmp_path / "spy-bin"
    spy_dir.mkdir()
    spy_uv = spy_dir / "uv"
    real_uv = subprocess.run(["which", "uv"], capture_output=True, text=True, check=True).stdout.strip()
    spy_uv.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            echo "$@" >> "{argv_log}"
            exec "{real_uv}" "$@"
            """
        )
    )
    spy_uv.chmod(0o755)

    result = _run_script(
        {
            "PACKAGE_NAME": "fakepkg",
            "PACKAGE_VERSION": "1.2.3",
            "EXTRA_UV_ARGS": f"--no-index --find-links {fake_wheel_dir}",
        },
        path_prefix=str(spy_dir),
    )

    assert result.returncode == 0, result.stderr
    logged = argv_log.read_text()
    assert "--dry-run" in logged
    assert "--system" in logged
    assert "--break-system-packages" in logged


# --- Retry policy: deterministic, fake `uv` stand-in ------------------------


@pytest.fixture
def fake_uv(tmp_path: Path):
    """Installs a fake `uv` on PATH that fails until the call count reaches
    FAKE_UV_SUCCEED_AT, so retry/backoff behavior is testable without real
    network calls or real elapsed time."""
    bin_dir = tmp_path / "fake-uv-bin"
    bin_dir.mkdir()
    counter_file = tmp_path / "call-count"
    script = bin_dir / "uv"
    script.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            set -euo pipefail
            counter_file="{counter_file}"
            succeed_at="${{FAKE_UV_SUCCEED_AT:-1}}"
            count=0
            [ -f "$counter_file" ] && count=$(cat "$counter_file")
            count=$((count + 1))
            echo "$count" > "$counter_file"
            if [ "$count" -ge "$succeed_at" ]; then
                echo "Resolved 1 package in 1ms"
                exit 0
            fi
            echo "  x No solution found when resolving dependencies:" >&2
            echo "  Because there is no version of fakepkg==9.9.9 [...] unsatisfiable." >&2
            exit 1
            """
        )
    )
    script.chmod(0o755)
    return bin_dir, counter_file


def _call_count(counter_file: Path) -> int:
    return int(counter_file.read_text()) if counter_file.exists() else 0


def test_retries_until_uv_resolution_succeeds(fake_uv) -> None:
    bin_dir, counter_file = fake_uv

    result = _run_script(
        {
            "PACKAGE_NAME": "fakepkg",
            "PACKAGE_VERSION": "9.9.9",
            "FAKE_UV_SUCCEED_AT": "3",
            "MAX_ATTEMPTS": "5",
        },
        path_prefix=str(bin_dir),
    )

    assert result.returncode == 0, result.stderr
    assert _call_count(counter_file) == 3


def test_fails_explicitly_after_bounded_attempts(fake_uv) -> None:
    bin_dir, counter_file = fake_uv

    result = _run_script(
        {
            "PACKAGE_NAME": "fakepkg",
            "PACKAGE_VERSION": "9.9.9",
            "FAKE_UV_SUCCEED_AT": "999",
            "MAX_ATTEMPTS": "3",
        },
        path_prefix=str(bin_dir),
    )

    assert result.returncode != 0
    assert _call_count(counter_file) == 3
    assert "never resolved" in (result.stdout + result.stderr)


def test_never_sleeps_past_the_last_attempt(fake_uv) -> None:
    import time

    bin_dir, _counter_file = fake_uv

    started = time.monotonic()
    result = _run_script(
        {
            "PACKAGE_NAME": "fakepkg",
            "PACKAGE_VERSION": "9.9.9",
            "FAKE_UV_SUCCEED_AT": "999",
            "MAX_ATTEMPTS": "2",
            "SLEEP_SECONDS": "5",
        },
        path_prefix=str(bin_dir),
    )
    elapsed = time.monotonic() - started

    assert result.returncode != 0
    # One sleep between attempt 1 and 2 (~5s) is expected; a second sleep
    # after the final failed attempt would push this past 10s.
    assert elapsed < 10, f"elapsed={elapsed:.1f}s suggests an extra sleep after the last attempt"

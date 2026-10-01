"""Python Script tool — run user-provided Python in a confined subprocess.

**Self-hosted only. Disabled by default.** Enable with the environment
variable ``ENABLE_PYTHON_SCRIPT_TOOL=true`` (``settings.enable_python_script_tool``).
A managed / multi-tenant deployment must leave it off: the flag being
``false`` *is* the cloud guard — there is no separate "edition" concept to
configure. When the flag is off the whole category is hidden from the tool
store and the catalogue (see ``tool_manager.ToolsStore.get_categories`` and
``tool_catalog._EXCLUDED_CATEGORIES``), and a direct call still refuses with
``FEATURE_DISABLED`` as defence in depth.

Confinement vs. isolation
-------------------------
This tool executes arbitrary code. The measures here *reduce blast radius*;
they are **not** a security boundary against a determined attacker:

* The script runs in a separate ``python -I`` (isolated) interpreter, in a
  throw-away working directory, with a **minimal environment** — the parent
  process's secrets (DB creds, API keys, OAuth tokens) are never inherited.
* POSIX resource limits cap CPU time, address space, file size and process
  count (best-effort; silently skipped where the platform lacks a limit).
* A wall-clock timeout kills the whole process group (``start_new_session`` +
  ``killpg``), so a runaway or a fork bomb cannot outlive the call.
* If the host process happens to run as root, the child drops to an
  unprivileged uid before executing the script.
* When network access is disabled, ``socket`` is neutered in the child. This
  is a convenience guard, trivially bypassable from inside the script
  (``ctypes``, ``os.system`` …). **Real network/file isolation requires an
  OS-level sandbox** — a locked-down container, namespaces and seccomp around
  the pod. Deploy this tool only where that is already in place. See
  ``SECURITY.md`` and issue #151.

Scope (v1)
----------
``script`` is Python source only. The issue also mentions "path to a script
file"; that is deliberately **not** implemented here because it would let a
caller read any file reachable by the worker. Package management
(``requirements.txt`` / pre-installed deps) is left to the self-host operator
who controls the image the worker runs in.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time

from apowerb.configs.settings import get_settings

# Child bootstrap, executed by a fresh ``python -I``. It reads a JSON payload
# from stdin, applies the confinement *inside the child* (so nothing here
# depends on a ``preexec_fn``, which is unsafe in the multi-threaded server),
# then execs the user script with ``inputs`` exposed as a global.
_CHILD_STUB = r"""
import json, os, resource, sys, traceback

payload = json.loads(sys.stdin.read())
script = payload["script"]
inputs = payload.get("inputs") or {}


def _limit(res_name, value):
    if value is None:
        return
    res = getattr(resource, res_name, None)
    if res is None:
        return
    try:
        resource.setrlimit(res, (value, value))
    except (ValueError, OSError):
        pass  # platform may not honour this limit (e.g. RLIMIT_AS on macOS)


_limit("RLIMIT_CPU", payload.get("cpu_seconds"))
_limit("RLIMIT_AS", payload.get("memory_bytes"))
_limit("RLIMIT_FSIZE", payload.get("fsize_bytes"))
_limit("RLIMIT_NPROC", payload.get("nproc"))

# Defence in depth: never run the script as root.
run_uid = payload.get("run_uid")
if run_uid is not None and hasattr(os, "geteuid") and os.geteuid() == 0:
    try:
        os.setgroups([])
    except (OSError, AttributeError):
        pass
    try:
        os.setgid(run_uid)
        os.setuid(run_uid)
    except OSError:
        sys.stderr.write("python_script: could not drop privileges\n")
        sys.exit(70)

# Convenience network guard — NOT a boundary (see module docstring).
if not payload.get("allow_network", False):
    import socket

    def _blocked(*_a, **_k):
        raise OSError("network access is disabled for this script")

    socket.socket = _blocked
    socket.create_connection = _blocked

glb = {"__name__": "__main__", "inputs": inputs}
try:
    exec(compile(script, "<python_script_tool>", "exec"), glb)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    sys.exit(1)
"""


def _minimal_env(home: str) -> dict:
    """A clean environment for the child — no inherited secrets."""
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": home,
        "TMPDIR": home,
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        # ``-I`` already ignores PYTHON* vars; we simply don't pass them.
    }


def tool_run_python_script(
    script: str,
    inputs: dict | None = None,
    timeout: int = 30,
) -> dict:
    """Execute a Python script in a confined subprocess (self-hosted only).

    The script runs in an isolated interpreter with a minimal environment, a
    wall-clock timeout and POSIX resource limits. The ``inputs`` mapping is
    exposed to the script as a global variable named ``inputs``; whatever the
    script prints is captured and returned.

    Args:
        script (str): Python source code to execute.
        inputs (dict): JSON-serialisable variables made available to the
            script as the global ``inputs``. Optional.
        timeout (int): Maximum wall-clock seconds before the script is killed.
            Capped by ``settings.python_script_max_timeout``. Default 30.

    Returns:
        dict: ``status`` ("success"/"error"), ``stdout``, ``stderr``,
        ``return_code``, ``duration_s`` and ``timed_out``. On a disabled
        feature or a bad argument, ``status`` is "error" with an
        ``error_code`` and ``error_message`` and the process is never touched.
    """
    settings = get_settings()
    if not settings.enable_python_script_tool:
        return {
            "status": "error",
            "error_code": "FEATURE_DISABLED",
            "error_message": (
                "The Python Script tool is disabled. It is available only on "
                "self-hosted deployments with ENABLE_PYTHON_SCRIPT_TOOL=true."
            ),
        }

    if not isinstance(script, str) or not script.strip():
        return {
            "status": "error",
            "error_code": "INVALID_SCRIPT",
            "error_message": "`script` must be a non-empty Python source string.",
        }
    if inputs is not None and not isinstance(inputs, dict):
        return {
            "status": "error",
            "error_code": "INVALID_INPUTS",
            "error_message": "`inputs` must be a JSON-serialisable object or null.",
        }

    try:
        requested = int(timeout)
    except (TypeError, ValueError):
        requested = 30
    max_timeout = max(1, int(settings.python_script_max_timeout))
    eff_timeout = max(1, min(requested, max_timeout))

    try:
        payload = json.dumps(
            {
                "script": script,
                "inputs": inputs or {},
                # Give the CPU limit a little slack over the wall clock so the
                # timeout (which also kills descendants) is the primary guard.
                "cpu_seconds": eff_timeout + 1,
                "memory_bytes": max(1, int(settings.python_script_memory_mb))
                * 1024
                * 1024,
                "fsize_bytes": max(1, int(settings.python_script_fsize_mb))
                * 1024
                * 1024,
                "nproc": max(1, int(settings.python_script_max_processes)),
                "run_uid": int(settings.python_script_run_uid),
                "allow_network": bool(settings.python_script_allow_network),
            }
        )
    except (TypeError, ValueError) as exc:
        return {
            "status": "error",
            "error_code": "INVALID_INPUTS",
            "error_message": f"`inputs` is not JSON-serialisable: {exc}",
        }

    workdir = tempfile.mkdtemp(prefix="python_script_")
    out_path = os.path.join(workdir, "stdout")
    err_path = os.path.join(workdir, "stderr")
    out_cap = max(1, int(settings.python_script_max_output_kb)) * 1024
    start = time.monotonic()
    timed_out = False
    try:
        # stdout/stderr go to FILES, not pipes: the child's writes are bounded
        # by RLIMIT_FSIZE and the parent reads back a capped amount, so a script
        # that streams output until the timeout cannot balloon the host's RAM.
        with open(out_path, "wb") as out_f, open(err_path, "wb") as err_f:
            proc = subprocess.Popen(  # noqa: S603 — interpreter path is ours, not user input
                [sys.executable, "-I", "-c", _CHILD_STUB],
                stdin=subprocess.PIPE,
                stdout=out_f,
                stderr=err_f,
                cwd=workdir,
                env=_minimal_env(workdir),
                start_new_session=True,  # own process group, so killpg reaches children
            )
            try:
                proc.communicate(input=payload.encode("utf-8"), timeout=eff_timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_group(proc)
                proc.communicate()
        duration = time.monotonic() - start
        return_code = proc.returncode
        stdout, out_truncated = _read_capped(out_path, out_cap)
        stderr, err_truncated = _read_capped(err_path, out_cap)
    except Exception as exc:  # pragma: no cover — defensive: never crash the host
        return {
            "status": "error",
            "error_code": "EXECUTION_ERROR",
            "error_message": f"Failed to run the script subprocess: {exc}",
            "duration_s": round(time.monotonic() - start, 3),
        }
    finally:
        _cleanup_dir(workdir)

    ok = (not timed_out) and return_code == 0
    result = {
        "status": "success" if ok else "error",
        "stdout": stdout,
        "stderr": stderr,
        "return_code": return_code,
        "duration_s": round(duration, 3),
        "timed_out": timed_out,
        "output_truncated": out_truncated or err_truncated,
    }
    if timed_out:
        result["error_code"] = "TIMEOUT"
        result["error_message"] = (
            f"Script exceeded the {eff_timeout}s timeout and was terminated."
        )
    elif not ok:
        result["error_code"] = "NON_ZERO_EXIT"
        result["error_message"] = (
            f"Script exited with code {return_code}. See stderr for the traceback."
        )
    return result


def _kill_group(proc: subprocess.Popen) -> None:
    """Kill the child's whole process group; ignore an already-dead child."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def _read_capped(path: str, max_bytes: int) -> tuple[str, bool]:
    """Read at most ``max_bytes`` from ``path``; report whether it was truncated."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return "", False
    with open(path, "rb") as fh:
        data = fh.read(max_bytes)
    text = data.decode("utf-8", errors="replace")
    if size > max_bytes:
        return text + f"\n...[output truncated, {size} bytes total]", True
    return text, False


def _cleanup_dir(path: str) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)

import asyncio
import tempfile
import os
import time
from logging import getLogger

from apowerb.configs.paths import runtime_root

logger = getLogger(__name__)


def _exec_workspace_root() -> str:
    """Racine des répertoires de travail passés à Docker.

    ⚠️ **Pas ``/tmp``.** Docker est installé depuis snap sur les VM, et snap
    confine ``/tmp`` : un bind mount d'un répertoire ``/tmp`` de l'hôte apparaît
    **vide** dans le conteneur. ``execute_artifact`` écrivait le code avec
    ``tempfile.TemporaryDirectory()``, donc sous ``/tmp``, et toute exécution
    échouait sur ``can't open file '/tmp/code/<nom>'`` — le conteneur démarrait
    bien, il ne voyait simplement aucun fichier.

    Mesuré sur la dev le 2026-08-04 : monté depuis ``/tmp``, le répertoire est
    vide ; le même fichier monté depuis ``/home/ubuntu`` s'exécute et rend sa
    sortie. La racine runtime est hors de ``/tmp`` sur tous les déploiements.
    """
    workspace = runtime_root() / "exec"
    workspace.mkdir(parents=True, exist_ok=True)
    return str(workspace)

# Language to Docker image mapping
LANGUAGE_IMAGES = {
    "python": "python:3.12-slim",
    "javascript": "node:22-slim",
    "js": "node:22-slim",
    "bash": "bash:5",
    "sh": "bash:5",
    "ruby": "ruby:3.3-slim",
    "go": "golang:1.22-alpine",
}

# Language to execution command mapping
LANGUAGE_COMMANDS = {
    "python": ["python", "/tmp/code/{filename}"],
    "javascript": ["node", "/tmp/code/{filename}"],
    "js": ["node", "/tmp/code/{filename}"],
    "bash": ["bash", "/tmp/code/{filename}"],
    "sh": ["sh", "/tmp/code/{filename}"],
    "ruby": ["ruby", "/tmp/code/{filename}"],
    "go": ["go", "run", "/tmp/code/{filename}"],
}


async def execute_artifact(
    code: str,
    language: str,
    filename: str = "main",
    timeout: int = 30,
    args: list[str] | None = None,
    stdin_data: str | None = None,
) -> dict:
    """Execute code in an ephemeral Docker container.

    Args:
        code: Source code to execute.
        language: Programming language.
        filename: Filename for the code file.
        timeout: Max execution time in seconds.
        args: Optional command-line arguments.
        stdin_data: Optional stdin input.

    Returns:
        dict with stdout, stderr, exit_code, duration_ms.
    """
    lang = language.lower().strip()
    image = LANGUAGE_IMAGES.get(lang)
    if not image:
        return {
            "stdout": "",
            "stderr": f"Unsupported language: {language}. Supported: {', '.join(LANGUAGE_IMAGES.keys())}",
            "exit_code": 1,
            "duration_ms": 0,
        }

    # os.path.basename() strips directory components but passes a bare "."
    # or ".." through unchanged (no "/" to strip). tmpdir below is a fresh,
    # randomly-named directory, so the practical impact is limited to an
    # IsADirectoryError, but validate defensively rather than rely on that.
    safe_filename = os.path.basename(filename)
    if not safe_filename or safe_filename in (".", ".."):
        return {
            "stdout": "",
            "stderr": "Invalid filename",
            "exit_code": 1,
            "duration_ms": 0,
        }

    # Build the command template
    cmd_template = LANGUAGE_COMMANDS.get(lang, [lang, "/tmp/code/{filename}"])
    cmd = [part.format(filename=safe_filename) for part in cmd_template]
    if args:
        cmd.extend(args)

    # Write code to a temp directory Docker can actually read (see
    # _exec_workspace_root: snap confines /tmp).
    with tempfile.TemporaryDirectory(dir=_exec_workspace_root()) as tmpdir:
        code_path = os.path.join(tmpdir, safe_filename)
        with open(code_path, "w", encoding="utf-8") as f:
            f.write(code)

        # Build docker run command
        docker_cmd = [
            "docker", "run",
            "--rm",
            "--network", "none",          # No network access for security
            "--memory", "256m",            # Memory limit
            "--cpus", "0.5",               # CPU limit
            "-v", f"{tmpdir}:/tmp/code:ro",  # Mount code read-only
            image,
        ] + cmd

        logger.info(f"[ARTIFACT_EXEC] Running: {' '.join(docker_cmd)}")
        start_time = time.time()

        try:
            process = await asyncio.create_subprocess_exec(
                *docker_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.PIPE if stdin_data else None,
            )

            stdout, stderr = await asyncio.wait_for(
                process.communicate(input=stdin_data.encode() if stdin_data else None),
                timeout=timeout,
            )

            duration_ms = int((time.time() - start_time) * 1000)

            return {
                "stdout": stdout.decode("utf-8", errors="replace"),
                "stderr": stderr.decode("utf-8", errors="replace"),
                "exit_code": process.returncode,
                "duration_ms": duration_ms,
            }

        except asyncio.TimeoutError:
            duration_ms = int((time.time() - start_time) * 1000)
            # Kill the container if it times out
            try:
                process.kill()
                await process.wait()
            except Exception:
                pass
            return {
                "stdout": "",
                "stderr": f"Execution timed out after {timeout}s",
                "exit_code": -1,
                "duration_ms": duration_ms,
            }
        except FileNotFoundError:
            return {
                "stdout": "",
                "stderr": "Docker is not installed or not available in PATH.",
                "exit_code": -1,
                "duration_ms": 0,
            }
        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            logger.error(f"[ARTIFACT_EXEC] Error: {e}", exc_info=True)
            return {
                "stdout": "",
                "stderr": str(e),
                "exit_code": -1,
                "duration_ms": duration_ms,
            }

import asyncio
import tempfile
import os
import time
from logging import getLogger

logger = getLogger(__name__)

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

    # Build the command template
    cmd_template = LANGUAGE_COMMANDS.get(lang, [lang, "/tmp/code/{filename}"])
    cmd = [part.format(filename=filename) for part in cmd_template]
    if args:
        cmd.extend(args)

    # Write code to a temp directory
    with tempfile.TemporaryDirectory() as tmpdir:
        code_path = os.path.join(tmpdir, filename)
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

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
from th2agent.auth.dependencies import get_current_user
from th2agent.users import schemas as user_schemas
from th2agent.configs.paths import artifacts_store_dir
from th2agent.core.artifact_executor import execute_artifact
from th2agent.helpers.ownership import enforce_user_id_match as _enforce_user_id_match
from logging import getLogger
import json
import os

logger = getLogger(__name__)
router = APIRouter()

def _artifacts_dir() -> str:
    """Racine des artefacts — la même que celle passée à ADK par ``main.py``.

    C'était une constante de module (``os.path.abspath("artifacts_store")``),
    donc doublement fausse depuis que le chemin est configurable : elle figeait
    le répertoire courant au moment de l'import, et ignorait
    ``ARTIFACTS_STORE_DIR`` / ``TH2AGENT_RUNTIME_ROOT``. ADK écrivait alors les
    artefacts à l'endroit configuré pendant que cet endpoint les cherchait dans
    le CWD — et répondait une liste vide, sans erreur.

    Passer par ``artifacts_store_dir()`` supprime la divergence par
    construction : les deux côtés lisent la même source.
    """
    return str(artifacts_store_dir())


class ExecuteRequest(BaseModel):
    args: Optional[list[str]] = None
    stdin: Optional[str] = None
    timeout: Optional[int] = 30


def _get_session_artifacts_dir(agent_name: str, user_id: str, session_id: str) -> str:
    """Get the filesystem path for a session's artifacts."""
    return os.path.join(_artifacts_dir(), agent_name, user_id, session_id)


@router.get("/artifacts/{agent_name}/{user_id}/{session_id}", tags=["artifacts"])
async def list_artifacts(
    agent_name: str,
    user_id: str,
    session_id: str,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """List all artifacts for a session."""
    _enforce_user_id_match(user_id, current_user)
    try:
        artifacts_dir = _get_session_artifacts_dir(agent_name, user_id, session_id)
        logger.info(f"[ARTIFACTS] Listing artifacts at: {artifacts_dir}")

        if not os.path.exists(artifacts_dir):
            return []

        artifacts = []
        for fname in os.listdir(artifacts_dir):
            fpath = os.path.join(artifacts_dir, fname)
            if not os.path.isfile(fpath):
                continue
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    data = json.loads(f.read())
                artifacts.append({
                    "filename": data.get("filename", fname),
                    "language": data.get("language", "text"),
                    "version": data.get("version", 1),
                    "source": "adk",
                })
            except (json.JSONDecodeError, IOError):
                artifacts.append({
                    "filename": fname,
                    "language": "text",
                    "version": 1,
                    "source": "adk",
                })

        return artifacts
    except Exception as e:
        logger.error(f"[ARTIFACTS] Error listing artifacts: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/artifacts/{agent_name}/{user_id}/{session_id}/{filename}", tags=["artifacts"])
async def get_artifact(
    agent_name: str,
    user_id: str,
    session_id: str,
    filename: str,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Get a specific artifact's content."""
    _enforce_user_id_match(user_id, current_user)
    artifacts_dir = _get_session_artifacts_dir(agent_name, user_id, session_id)
    # Sanitize filename against path traversal (see M8 in security audit)
    safe_filename = os.path.basename(filename)
    fpath = os.path.join(artifacts_dir, safe_filename)

    if not os.path.exists(fpath):
        raise HTTPException(status_code=404, detail=f"Artifact '{filename}' not found")

    try:
        with open(fpath, "r", encoding="utf-8") as f:
            content = f.read()
        # Try to parse as JSON artifact
        try:
            data = json.loads(content)
            return {
                "filename": data.get("filename", filename),
                "language": data.get("language", "text"),
                "code": data.get("code", content),
                "version": data.get("version", 1),
                "source": "adk",
            }
        except json.JSONDecodeError:
            return {
                "filename": filename,
                "language": "text",
                "code": content,
                "version": 1,
                "source": "adk",
            }
    except IOError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/artifacts/{agent_name}/{user_id}/{session_id}/{filename}/execute", tags=["artifacts"])
async def execute_artifact_endpoint(
    agent_name: str,
    user_id: str,
    session_id: str,
    filename: str,
    request: ExecuteRequest = ExecuteRequest(),
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Execute an artifact in a Docker container."""
    _enforce_user_id_match(user_id, current_user)
    artifacts_dir = _get_session_artifacts_dir(agent_name, user_id, session_id)
    safe_filename = os.path.basename(filename)
    fpath = os.path.join(artifacts_dir, safe_filename)

    if not os.path.exists(fpath):
        raise HTTPException(status_code=404, detail=f"Artifact '{safe_filename}' not found")

    try:
        with open(fpath, "r", encoding="utf-8") as f:
            content = f.read()

        # Parse artifact data
        try:
            data = json.loads(content)
            code = data.get("code", content)
            language = data.get("language", "text")
        except json.JSONDecodeError:
            code = content
            language = _guess_language(safe_filename)

        result = await execute_artifact(
            code=code,
            language=language,
            filename=safe_filename,
            timeout=request.timeout or 30,
            args=request.args,
            stdin_data=request.stdin,
        )

        return result

    except IOError as e:
        raise HTTPException(status_code=500, detail=str(e))


def _guess_language(filename: str) -> str:
    """Guess language from file extension."""
    ext_map = {
        ".py": "python",
        ".js": "javascript",
        ".sh": "bash",
        ".rb": "ruby",
        ".go": "go",
    }
    _, ext = os.path.splitext(filename)
    return ext_map.get(ext, "text")

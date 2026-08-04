from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
from apowerb.auth.dependencies import get_current_user
from apowerb.users import schemas as user_schemas
from apowerb.configs.paths import artifacts_store_dir
from apowerb.core.artifact_executor import execute_artifact
from apowerb.helpers.ownership import enforce_user_id_match as _enforce_user_id_match
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


def _get_session_artifacts_dir(user_id: str, session_id: str) -> str:
    """Répertoire où ADK range les artefacts d'une session.

    Le layout réel, relevé sur la dev le 2026-08-04 après deux appels à
    ``tool_save_code_artifact`` :

        artifacts_store/users/<user>/sessions/<session>/artifacts/
            fizzbuzz.py/versions/0/fizzbuzz.py
            fizzbuzz.py/versions/0/metadata.json

    Cet endpoint construisait ``<racine>/<agent>/<user>/<session>`` et le
    listait à plat. Ce répertoire n'existe jamais : ``os.path.exists`` était
    faux et la route renvoyait ``[]`` — sans erreur ni log, un écran vide pour
    des artefacts pourtant présents sur le disque.

    ⚠️ Le **nom de l'agent n'apparaît pas** dans le chemin réel : ADK borne les
    artefacts à (utilisateur, session). Il reste dans la signature des routes
    pour ne pas casser les appelants, mais il n'entre pas dans la résolution.
    """
    return os.path.join(
        _artifacts_dir(), "users", user_id, "sessions", session_id, "artifacts"
    )


def _latest_version_dir(artifact_dir: str):
    """Renvoie ``(version, chemin)`` de la version la plus récente, ou ``None``.

    ⚠️ Le tri est **numérique** : en ordre lexical ``"10" < "2"``, donc la
    version 10 d'un artefact édité dix fois serait ignorée au profit de la 2.
    """
    versions_root = os.path.join(artifact_dir, "versions")
    if not os.path.isdir(versions_root):
        return None

    versions = [d for d in os.listdir(versions_root) if d.isdigit()]
    if not versions:
        return None

    latest = max(versions, key=int)
    return int(latest), os.path.join(versions_root, latest)


def _read_artifact_payload(version_dir: str, name: str) -> dict:
    """Lit le corps d'un artefact.

    ADK dépose deux fichiers par version : l'artefact lui-même, qui porte le
    nom de l'artefact, et un ``metadata.json`` qui lui appartient. Le premier
    contient ce qu'a écrit ``tool_save_code_artifact`` :
    ``{"filename", "language", "code"}``. Un artefact produit autrement peut
    n'être que du texte brut — d'où le repli.
    """
    path = os.path.join(version_dir, name)
    if not os.path.isfile(path):
        return {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except (IOError, UnicodeDecodeError):
        return {}

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return {"code": content}

    return data if isinstance(data, dict) else {"code": content}


def _resolve_artifact(user_id: str, session_id: str, filename: str):
    """Résout un artefact vers ``(version, dossier_de_version, contenu)``."""
    safe_name = os.path.basename(filename)
    artifact_dir = os.path.join(
        _get_session_artifacts_dir(user_id, session_id), safe_name
    )
    found = _latest_version_dir(artifact_dir)
    if found is None:
        return None

    version, version_dir = found
    return version, version_dir, _read_artifact_payload(version_dir, safe_name)


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
        artifacts_dir = _get_session_artifacts_dir(user_id, session_id)
        logger.info(f"[ARTIFACTS] Listing artifacts at: {artifacts_dir}")

        if not os.path.isdir(artifacts_dir):
            return []

        artifacts = []
        for name in sorted(os.listdir(artifacts_dir)):
            found = _latest_version_dir(os.path.join(artifacts_dir, name))
            if found is None:
                continue

            version, version_dir = found
            data = _read_artifact_payload(version_dir, name)
            artifacts.append({
                "filename": data.get("filename", name),
                "language": data.get("language", "text"),
                "version": version,
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

    resolved = _resolve_artifact(user_id, session_id, filename)
    if resolved is None:
        raise HTTPException(status_code=404, detail=f"Artifact '{filename}' not found")

    version, _version_dir, data = resolved
    safe_name = os.path.basename(filename)
    return {
        "filename": data.get("filename", safe_name),
        "language": data.get("language", "text"),
        "code": data.get("code", ""),
        "version": version,
        "source": "adk",
    }


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

    safe_filename = os.path.basename(filename)
    resolved = _resolve_artifact(user_id, session_id, safe_filename)
    if resolved is None:
        raise HTTPException(status_code=404, detail=f"Artifact '{safe_filename}' not found")

    _version, _version_dir, data = resolved
    code = data.get("code", "")
    language = data.get("language") or _guess_language(safe_filename)

    result = await execute_artifact(
        code=code,
        language=language,
        filename=safe_filename,
        timeout=request.timeout or 30,
        args=request.args,
        stdin_data=request.stdin,
    )

    return result


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

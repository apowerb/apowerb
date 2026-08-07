import asyncio
import json
import os
from logging import getLogger
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from apowerb.artifacts.input_scope import SHARED_INPUT_SCOPE
from apowerb.artifacts.languages import language_for_filename
from apowerb.artifacts.library import build_library
from apowerb.artifacts.s3_artifact_service import S3ArtifactService
from apowerb.auth.dependencies import get_current_user
from apowerb.configs.artifact_service_config import is_s3_artifact_storage_configured
from apowerb.helpers.safe_paths import contained_path
from apowerb.configs.paths import artifacts_store_dir
from apowerb.configs.settings import get_settings
from apowerb.core.artifact_executor import execute_artifact
from apowerb.helpers.ownership import enforce_user_id_match as _enforce_user_id_match
from apowerb.users import schemas as user_schemas

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


# -- artifact kinds --------------------------------------------------------
#
# Farid, 05/08: uploads must show up in the Artifacts tab next to generated
# artifacts, with a tag to tell them apart. Both live in the same bucket
# under the same key scheme (apowerb.artifacts.s3_artifact_service), only
# the "input"/"output" segment differs -- so the tab lists them together and
# this field is what the UI filters on.
KIND_INPUT = "input"
KIND_OUTPUT = "output"
# Files written before the artifact layout existed, under uploads/{agent}/.
# 455 of them on the dev bucket: uploads from the old flow and everything
# create_downloadable_file produced until it started writing output
# artifacts. Nothing distinguishes the two after the fact, so claiming
# either kind would be a guess -- they get their own, and the tab says so.
KIND_LEGACY = "legacy"

# A session can hold an input and an output under the same filename (upload
# report.html, have the agent regenerate report.html). The listing keeps
# both -- they differ by kind -- but a read by filename alone would be
# ambiguous, hence the optional ?kind= on the single-artifact routes.
_KINDS = (KIND_INPUT, KIND_OUTPUT, KIND_LEGACY)


class ExecuteRequest(BaseModel):
    args: Optional[list[str]] = None
    stdin: Optional[str] = None
    timeout: Optional[int] = 30


# Unlike agent_name/user_id here, session_id was never validated before
# being joined into the artifacts path, so a single ".." segment (no "/"
# required — a plain path parameter already forbids "/") could walk the
# lookup up one directory level. This endpoint deliberately tolerates
# unknown/free-form session_id values (test_unknown_session_stays_empty_
# not_an_error: an unrecognised session just yields an empty artifact list,
# not a 400), unlike the stricter "session_<digits>" formats enforced
# elsewhere (rag/validators.py, core/guardrails.py) — so only the actual
# traversal values are rejected here, not the whole free-form space.
_UNSAFE_SESSION_ID = (".", "..")


def _validate_session_id(session_id: str) -> None:
    if not session_id or session_id in _UNSAFE_SESSION_ID:
        raise HTTPException(status_code=400, detail="Invalid session_id format")


def _safe_path_component(name: str) -> str:
    """os.path.basename() strips directory components but passes a bare
    "." or ".." straight through unchanged (there's no "/" to strip), which
    still resolves to the current/parent directory when joined. Reject that
    case explicitly."""
    safe = os.path.basename(name)
    if not safe or safe in (".", ".."):
        raise HTTPException(status_code=400, detail="Invalid filename")
    return safe


# -- storage backend selection -------------------------------------------
#
# ADK writes artifacts to S3 when STORAGE_MODE=S3 is fully configured (see
# apowerb.configs.artifact_service_config, wired in main.py) -- true on both
# dev and prod. This router must read from wherever ADK actually wrote, or
# the Artifacts tab renders empty while the objects sit in the bucket: the
# same failure mode PR #17 already fixed once for the local-disk layout.


def _s3_artifacts_active() -> bool:
    return is_s3_artifact_storage_configured(get_settings())


def _get_session_artifacts_dir(user_id: str, session_id: str) -> str:
    """Répertoire où ADK range les artefacts d'une session, sur disque.

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

    Only used for the file:// backend -- see ``_list_artifacts_s3`` /
    ``_resolve_artifact_s3`` for the S3 counterpart, where the key is scoped
    by (agent_name, session_id) instead: user_id has no role there beyond
    authorization (``_enforce_user_id_match``), already enforced by callers
    before either branch runs.
    """
    return contained_path(
        _artifacts_dir(), "users", user_id, "sessions", session_id, "artifacts"
    )


def _latest_version_dir(artifact_dir: str):
    """Renvoie ``(version, chemin)`` de la version la plus récente, ou ``None``.

    ⚠️ Le tri est **numérique** : en ordre lexical ``"10" < "2"``, donc la
    version 10 d'un artefact édité dix fois serait ignorée au profit de la 2.
    """
    versions_root = contained_path(artifact_dir, "versions")
    if not os.path.isdir(versions_root):
        return None

    versions = [d for d in os.listdir(versions_root) if d.isdigit()]
    if not versions:
        return None

    latest = max(versions, key=int)
    return int(latest), contained_path(versions_root, latest)


def _read_artifact_payload(version_dir: str, name: str) -> dict:
    """Lit le corps d'un artefact sur disque.

    ADK dépose deux fichiers par version : l'artefact lui-même, qui porte le
    nom de l'artefact, et un ``metadata.json`` qui lui appartient. Le premier
    contient ce qu'a écrit ``tool_save_code_artifact`` :
    ``{"filename", "language", "code"}``. Un artefact produit autrement peut
    n'être que du texte brut — d'où le repli.
    """
    path = contained_path(version_dir, name)
    if not os.path.isfile(path):
        return {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except (IOError, UnicodeDecodeError):
        return {}

    return _parse_artifact_content(content)


def _parse_artifact_content(content: str) -> dict:
    """Shared payload parsing for both backends: ``tool_save_code_artifact``
    writes a ``{"filename", "language", "code"}`` JSON body regardless of
    where it lands (disk or S3) -- a plain-text artifact falls back to
    ``{"code": content}``."""
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return {"code": content}

    return data if isinstance(data, dict) else {"code": content}


def _resolve_artifact(user_id: str, session_id: str, filename: str):
    """Résout un artefact depuis le disque vers ``(version, contenu)``."""
    safe_name = _safe_path_component(filename)
    artifact_dir = os.path.join(
        _get_session_artifacts_dir(user_id, session_id), safe_name
    )
    found = _latest_version_dir(artifact_dir)
    if found is None:
        return None

    version, version_dir = found
    return version, _read_artifact_payload(version_dir, safe_name)


# -- S3 backend ------------------------------------------------------------
#
# Reuses S3ArtifactService (apowerb.artifacts.s3_artifact_service) rather
# than re-deriving the key layout here: it already imposes and tests
# "artifacts/{app_name}/{session_id}/output/{filename}/{version}/{filename}"
# against the real bucket (tests/test_s3_artifact_service_real_bucket.py).
# user_id is threaded through for API compatibility with the service (it
# only matters for the "user:"-namespaced scheme, unused by this router);
# the actual scoping is (agent_name, session_id).


def _parse_artifact_part(part) -> dict:
    """S3ArtifactService.load_artifact always returns content via
    ``inline_data`` -- ``Part.text`` is never populated on load, verified
    against the real ``google.genai.types.Part`` behavior in
    tests/test_s3_artifact_service.py."""
    raw = part.inline_data.data if part.inline_data else b""
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {}
    return _parse_artifact_content(content)


async def _resolve_artifact_s3(
    service: S3ArtifactService, agent_name: str, user_id: str, session_id: str,
    filename: str,
):
    """Résout un artefact depuis S3 vers ``(version, contenu)``, ou ``None``."""
    safe_name = _safe_path_component(filename)
    versions = await service.list_versions(
        app_name=agent_name, user_id=user_id, session_id=session_id, filename=safe_name,
    )
    if not versions:
        return None

    version = max(versions)
    part = await service.load_artifact(
        app_name=agent_name, user_id=user_id, session_id=session_id,
        filename=safe_name, version=version,
    )
    if part is None:
        return None

    return version, _parse_artifact_part(part)


async def _list_artifacts_s3(agent_name: str, user_id: str, session_id: str) -> list[dict]:
    service = S3ArtifactService()
    filenames = await service.list_artifact_keys(
        app_name=agent_name, user_id=user_id, session_id=session_id,
    )

    artifacts = []
    for name in sorted(filenames):
        resolved = await _resolve_artifact_s3(service, agent_name, user_id, session_id, name)
        if resolved is None:
            continue

        version, data = resolved
        artifacts.append({
            "filename": data.get("filename", name),
            "language": data.get("language", "text"),
            "version": version,
            "source": "adk",
            "kind": KIND_OUTPUT,
        })

    artifacts.extend(await _list_input_artifacts_s3(service, agent_name, session_id))
    # Only in the shared scope. A legacy file carries no session -- the old
    # path never had one -- so returning it for every session would show the
    # same document once per conversation of that agent.
    if session_id == SHARED_INPUT_SCOPE:
        artifacts.extend(
            await _list_legacy_files(agent_name, {a["filename"] for a in artifacts})
        )

    return artifacts


async def _list_legacy_files(agent_name: str, already_listed: set[str]) -> list[dict]:
    """Files under ``uploads/{agent}/``, which no writer targets any more.

    Listed against the agent rather than a session -- the old layout carried
    none -- so they appear in every session of that agent. Duplicating a name
    that already exists as a real artifact would show the same file twice, so
    those are dropped: the artifact is the better record of the two.
    """
    from apowerb.storage.s3 import list_files_in_s3

    prefix = f"uploads/{agent_name}/"
    entries = []
    for key in await asyncio.to_thread(lambda: list(list_files_in_s3(prefix=prefix))):
        name = key[len(prefix):]
        if not name or "/" in name or name in already_listed:
            continue
        entries.append({
            "filename": name,
            "language": _guess_language(name),
            "version": 0,
            "source": "legacy",
            "kind": KIND_LEGACY,
        })

    return sorted(entries, key=lambda e: e["filename"])


async def _list_input_artifacts_s3(
    service: S3ArtifactService, agent_name: str, session_id: str,
) -> list[dict]:
    """Uploads stored for this session, as listing entries.

    Only the key layout is read here -- never the object bodies. An upload is
    an arbitrary file (PDF, archive, image); downloading each one to build a
    listing would transfer the whole session on every page load, and its
    bytes say nothing the listing shows anyway.
    """
    filenames = await service.list_input_artifact_filenames(
        app_name=agent_name, session_id=session_id,
    )

    artifacts = []
    for name in sorted(filenames):
        versions = await service.list_input_versions(
            app_name=agent_name, session_id=session_id, filename=name,
        )
        if not versions:
            continue

        artifacts.append({
            "filename": name,
            "language": _guess_language(name),
            "version": max(versions),
            "source": "upload",
            "kind": KIND_INPUT,
        })

    return artifacts


async def _resolve_input_artifact_s3(
    service: S3ArtifactService, agent_name: str, session_id: str, filename: str,
):
    """Résout un upload depuis S3 vers ``(version, contenu)``, ou ``None``.

    An upload is raw bytes, not the ``{"filename", "language", "code"}`` body
    ``tool_save_code_artifact`` writes. Text decodes into the same ``code``
    field the tab already renders; anything else is reported as binary rather
    than mangled into replacement characters.
    """
    safe_name = _safe_path_component(filename)
    loaded = await service.load_input_artifact(
        app_name=agent_name, session_id=session_id, filename=safe_name,
    )
    if loaded is None:
        return None

    try:
        code = loaded["data"].decode("utf-8")
    except UnicodeDecodeError:
        return loaded["version"], {"binary": True, "code": ""}

    return loaded["version"], {"code": code}


@router.get("/artifacts/library", tags=["artifacts"])
async def list_artifact_library(
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Every artifact the caller owns, in one call.

    Declared before the "/{agent_name}/{user_id}/{session_id}" route on
    purpose: FastAPI matches in declaration order, and "library" would
    otherwise be read as an agent name.

    The tab used to ask once per session plus once per agent — 386 sessions
    on a real dev account, ~200 ms each even when the session held nothing,
    so about fifteen seconds of waiting. Everything it displays is already
    in the S3 keys, so this builds the whole answer from listings and
    downloads no object body at all.
    """
    if not _s3_artifacts_active():
        # The file:// backend has no equivalent sweep: it is a development
        # fallback, and the per-session route still serves it.
        return {"items": [], "supported": False}

    agents = await asyncio.to_thread(_owned_agents, current_user.email)
    items = await asyncio.to_thread(build_library, agents)
    return {"items": items, "supported": True}


def _owned_agents(owner_email: str) -> dict[str, str]:
    """Folder name -> display name, for the agents this user owns.

    The folder is what the S3 key carries ("agent12"); the display name is
    what the screen shows.
    """
    from apowerb.core.agent_main import agent_store

    table = agent_store.agent_table
    rows = agent_store.get_list_agents(
        table.select().where(table.c.owner_id == owner_email)
    )

    agents: dict[str, str] = {}
    for row in rows:
        data = row._asdict()
        folder = f"agent{data['agent_id']}"
        agents[folder] = data.get("agent_name") or folder
    return agents


@router.get("/artifacts/{agent_name}/{user_id}/{session_id}", tags=["artifacts"])
async def list_artifacts(
    agent_name: str,
    user_id: str,
    session_id: str,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """List all artifacts for a session."""
    _enforce_user_id_match(user_id, current_user)
    _validate_session_id(session_id)
    try:
        if _s3_artifacts_active():
            return await _list_artifacts_s3(agent_name, user_id, session_id)

        artifacts_dir = _get_session_artifacts_dir(user_id, session_id)
        logger.info(f"[ARTIFACTS] Listing artifacts at: {artifacts_dir}")

        if not os.path.isdir(artifacts_dir):
            return []

        artifacts = []
        for name in sorted(os.listdir(artifacts_dir)):
            found = _latest_version_dir(contained_path(artifacts_dir, name))
            if found is None:
                continue

            version, version_dir = found
            data = _read_artifact_payload(version_dir, name)
            artifacts.append({
                "filename": data.get("filename", name),
                "language": data.get("language", "text"),
                "version": version,
                "source": "adk",
                # The file:// backend has no input side: uploads only reach
                # the artifact store through S3 (routers/files.py, PR #30).
                "kind": KIND_OUTPUT,
            })

        return artifacts
    except Exception as e:
        logger.error(f"[ARTIFACTS] Error listing artifacts: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


def _validate_kind(kind: Optional[str]) -> Optional[str]:
    if kind is not None and kind not in _KINDS:
        raise HTTPException(status_code=400, detail="Invalid kind")
    return kind


async def _resolve_any_artifact_s3(
    agent_name: str, user_id: str, session_id: str, filename: str,
    kind: Optional[str],
):
    """Résout un artefact S3, sortie ou entrée, vers ``(kind, version, contenu)``.

    Without ``kind`` the generated artifact wins and an upload only answers
    when no output carries that name -- the behaviour every existing caller
    already relies on.
    """
    service = S3ArtifactService()

    if kind != KIND_INPUT:
        resolved = await _resolve_artifact_s3(
            service, agent_name, user_id, session_id, filename
        )
        if resolved is not None:
            return (KIND_OUTPUT, *resolved)
        if kind == KIND_OUTPUT:
            return None

    if kind != KIND_LEGACY:
        resolved = await _resolve_input_artifact_s3(
            service, agent_name, session_id, filename
        )
        if resolved is not None:
            return (KIND_INPUT, *resolved)
        if kind == KIND_INPUT:
            return None

    resolved = await _resolve_legacy_file(agent_name, filename)
    return None if resolved is None else (KIND_LEGACY, *resolved)


async def _resolve_legacy_file(agent_name: str, filename: str):
    """Résout un fichier de l'ancien emplacement vers ``(version, contenu)``.

    The old layout has no versions, hence the constant 0 -- the tab needs a
    number and there was never more than one file per name.
    """
    from apowerb.storage.s3 import download_file_from_s3, file_exists_in_s3

    safe_name = _safe_path_component(filename)
    key = f"uploads/{agent_name}/{safe_name}"
    if not await asyncio.to_thread(file_exists_in_s3, key):
        return None

    raw = await asyncio.to_thread(download_file_from_s3, key)
    try:
        return 0, {"code": raw.decode("utf-8")}
    except UnicodeDecodeError:
        return 0, {"binary": True, "code": ""}


@router.get("/artifacts/{agent_name}/{user_id}/{session_id}/{filename}", tags=["artifacts"])
async def get_artifact(
    agent_name: str,
    user_id: str,
    session_id: str,
    filename: str,
    kind: Optional[str] = Query(None),
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Get a specific artifact's content."""
    _enforce_user_id_match(user_id, current_user)
    _validate_session_id(session_id)
    _validate_kind(kind)

    if _s3_artifacts_active():
        found = await _resolve_any_artifact_s3(
            agent_name, user_id, session_id, filename, kind
        )
    else:
        resolved = _resolve_artifact(user_id, session_id, filename)
        found = None if resolved is None else (KIND_OUTPUT, *resolved)

    if found is None:
        raise HTTPException(status_code=404, detail=f"Artifact '{filename}' not found")

    resolved_kind, version, data = found
    safe_name = _safe_path_component(filename)
    return {
        "filename": data.get("filename", safe_name),
        "language": data.get("language") or _guess_language(safe_name),
        "code": data.get("code", ""),
        "version": version,
        "source": "adk" if resolved_kind == KIND_OUTPUT else "upload",
        "kind": resolved_kind,
        "binary": bool(data.get("binary")),
    }


@router.post("/artifacts/{agent_name}/{user_id}/{session_id}/{filename}/execute", tags=["artifacts"])
async def execute_artifact_endpoint(
    agent_name: str,
    user_id: str,
    session_id: str,
    filename: str,
    request: ExecuteRequest = ExecuteRequest(),
    kind: Optional[str] = Query(None),
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Execute an artifact in a Docker container."""
    _enforce_user_id_match(user_id, current_user)
    _validate_session_id(session_id)
    _validate_kind(kind)

    safe_filename = _safe_path_component(filename)

    if _s3_artifacts_active():
        found = await _resolve_any_artifact_s3(
            agent_name, user_id, session_id, safe_filename, kind
        )
        resolved = None if found is None else found[1:]
    else:
        resolved = _resolve_artifact(user_id, session_id, safe_filename)

    if resolved is None:
        raise HTTPException(status_code=404, detail=f"Artifact '{safe_filename}' not found")

    _version, data = resolved
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
    """Guess language from file extension.

    Delegates to the shared table: this one knew five extensions and not
    ".html", so a generated report was listed as plain text and lost its
    preview.
    """
    return language_for_filename(filename)

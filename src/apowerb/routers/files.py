import asyncio
import json
import os
import re
import shutil
import tempfile
from io import BytesIO
from fastapi import APIRouter, HTTPException, Depends, UploadFile, File, Form, Query, Request
from fastapi.responses import FileResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from logging import getLogger
from pydantic import BaseModel

from apowerb.auth.dependencies import get_current_user, get_optional_user
from apowerb.users import schemas as user_schemas
from apowerb.helpers.security import generate_download_token, verify_download_token
from apowerb.configs.paths import uploads_dir
from apowerb.configs.settings import get_settings
from apowerb.configs.artifact_service_config import is_s3_artifact_storage_configured
from apowerb.artifacts.s3_artifact_service import S3ArtifactService
from apowerb.artifacts.input_scope import resolve_input_session_id
from apowerb.artifacts.file_lookup import available_filenames, read_file_bytes
from apowerb.helpers.ownership import validate_agent_ownership as _validate_agent_ownership

logger = getLogger(__name__)
router = APIRouter()

security = HTTPBearer(auto_error=False)

CHUNK_SIZE = 65536  # 64 KB

# upload_id is client-generated and, unlike agent_id/filename, was never
# format-checked before being joined into a filesystem path. A value like
# "../../../../etc" let any authenticated caller create directories and
# write/read chunk files outside uploads_dir()/_chunks (path traversal), and
# since chunks are re-read by upload_id alone in upload_complete, a guessed
# upload_id also let one user pull another user's in-flight chunk data.
_UPLOAD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _validate_upload_id(upload_id: str) -> None:
    if not _UPLOAD_ID_RE.match(upload_id):
        raise HTTPException(status_code=400, detail="Invalid upload_id format")


# agent_id reaches a filesystem path in four places here and was never
# format-checked, unlike upload_id above. `_validate_agent_ownership()` proves
# the caller owns the agent; it says nothing about the *shape* of the string,
# so an id carrying ".." passes ownership and still walks out of
# uploads_dir(). CodeQL reports it as py/path-injection (high).
#
# Rejecting outright rather than sanitising: os.path.basename("..") returns
# ".." unchanged -- there is no separator to strip -- so stripping directory
# components is not enough, as the artifacts router found on 2026-08-04.
_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _safe_agent_id(agent_id: str) -> str:
    """Return the id only if it is safe to join into a path.

    Returns rather than merely raising so callers use the *checked* value:
    a guard that only raises leaves the raw parameter in scope, and CodeQL
    keeps reporting py/path-injection because the tainted value is what
    reaches the path expression. Same shape as ``_safe_path_component`` in
    routers/artifacts.py.
    """
    if not _AGENT_ID_RE.match(agent_id):
        raise HTTPException(status_code=400, detail="Invalid agent_id format")
    return agent_id


def _contained_upload_path(*parts: str) -> str:
    """Join *parts* under uploads_dir() and prove the result stays inside it.

    The format guards above reject the values we know how to name, but they
    cannot see the filesystem: a symlink planted inside uploads_dir() points
    somewhere else while every component still matches ``[A-Za-z0-9_-]``.
    Resolving the candidate and comparing it to the resolved base closes that,
    and it is the only construction CodeQL's py/path-injection accepts as a
    sanitiser -- it tracks two states and requires a normalisation call
    (``os.path.realpath`` here) followed by a ``.startswith()`` check on the
    branch that reaches the sink. Two regex-guard revisions were reported
    anyway, correctly: a rejected shape is not a proven location.

    Written in the positive form (check, then return) so the value callers
    receive only ever comes from the branch where containment held.
    """
    base = os.path.realpath(str(uploads_dir()))
    candidate = os.path.realpath(os.path.join(base, *parts))
    if candidate.startswith(base + os.sep):
        return candidate
    raise HTTPException(status_code=400, detail="Invalid path")


class UploadCompleteRequest(BaseModel):
    upload_id: str
    agent_id: str
    filename: str
    total_chunks: int
    session_id: str | None = None


@router.post("/files/upload", tags=["files"])
async def upload_file(
    file: UploadFile = File(...),
    agent_id: str = Form(...),
    session_id: str | None = Form(None),
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Upload a file for an agent to access.

    Written as an input artifact (``artifacts/{agent}/{session-or-_shared}
    /input/...``) when S3 storage is active, alongside the existing output
    artifacts (#28/#29) in the same bucket. ``session_id`` is optional --
    no current caller sends one -- and an absent one scopes the upload to
    a shared, agent-wide namespace rather than a real session (Option C).
    """
    agent_id = _safe_agent_id(agent_id)
    await _validate_agent_ownership(agent_id, current_user)

    # Sanitize filename
    filename = os.path.basename(file.filename)
    if not filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    try:
        content = await file.read()
        total_size = len(content)
        settings = get_settings()

        if is_s3_artifact_storage_configured(settings):
            resolved_session_id = resolve_input_session_id(session_id)
            await S3ArtifactService().save_input_artifact(
                app_name=agent_id,
                session_id=resolved_session_id,
                filename=filename,
                data=content,
                content_type=file.content_type,
            )
        else:
            agent_dir = _contained_upload_path(agent_id)
            os.makedirs(agent_dir, exist_ok=True)
            file_path = _contained_upload_path(agent_id, filename)
            with open(file_path, "wb") as f:
                f.write(content)

        logger.info(f"[FILES] Uploaded {filename} for {agent_id} ({total_size} bytes)")

        return {
            "filename": filename,
            "agent_id": agent_id,
            "size": total_size,
            "path": f"/api/files/{agent_id}/{filename}",
        }
    except Exception as e:
        logger.error(f"[FILES] Upload failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/files/upload-chunk", tags=["files"])
async def upload_chunk(
    upload_id: str = Form(...),
    agent_id: str = Form(...),
    chunk_index: int = Form(...),
    total_chunks: int = Form(...),
    filename: str = Form(...),
    chunk: UploadFile = File(...),
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Upload a single chunk of a large file."""
    agent_id = _safe_agent_id(agent_id)
    await _validate_agent_ownership(agent_id, current_user)
    _validate_upload_id(upload_id)

    safe_filename = os.path.basename(filename)
    if not safe_filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    if chunk_index < 0 or chunk_index >= total_chunks:
        raise HTTPException(status_code=400, detail="Invalid chunk_index")

    chunk_dir = _contained_upload_path("_chunks", upload_id)
    os.makedirs(chunk_dir, exist_ok=True)

    chunk_path = _contained_upload_path(
        "_chunks", upload_id, f"{chunk_index}.part"
    )

    try:
        with open(chunk_path, "wb") as f:
            while data := await chunk.read(CHUNK_SIZE):
                f.write(data)

        logger.info(
            f"[FILES] Chunk {chunk_index}/{total_chunks - 1} received for "
            f"upload_id={upload_id} ({safe_filename})"
        )

        return {
            "upload_id": upload_id,
            "chunk_index": chunk_index,
            "received": True,
        }
    except Exception as e:
        logger.error(f"[FILES] Chunk upload failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/files/upload-complete", tags=["files"])
async def upload_complete(
    body: UploadCompleteRequest,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Assemble all uploaded chunks into the final file."""
    safe_agent_id = _safe_agent_id(body.agent_id)
    await _validate_agent_ownership(body.agent_id, current_user)
    _validate_upload_id(body.upload_id)

    safe_filename = os.path.basename(body.filename)
    if not safe_filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    chunk_dir = _contained_upload_path("_chunks", body.upload_id)

    # Verify all chunks exist
    missing = []
    for i in range(body.total_chunks):
        part_path = os.path.join(chunk_dir, f"{i}.part")
        if not os.path.exists(part_path):
            missing.append(i)

    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Missing chunks: {missing}",
        )

    settings = get_settings()
    use_s3 = is_s3_artifact_storage_configured(settings)

    try:
        total_size = 0
        if use_s3:
            buffer = BytesIO()
            for i in range(body.total_chunks):
                part_path = os.path.join(chunk_dir, f"{i}.part")
                with open(part_path, "rb") as part_f:
                    while data := part_f.read(CHUNK_SIZE):
                        buffer.write(data)
                        total_size += len(data)

            resolved_session_id = resolve_input_session_id(body.session_id)
            await S3ArtifactService().save_input_artifact(
                app_name=safe_agent_id,
                session_id=resolved_session_id,
                filename=safe_filename,
                data=buffer.getvalue(),
            )
        else:
            agent_dir = _contained_upload_path(safe_agent_id)
            os.makedirs(agent_dir, exist_ok=True)
            final_path = _contained_upload_path(safe_agent_id, safe_filename)

            with open(final_path, "wb") as out_f:
                for i in range(body.total_chunks):
                    part_path = os.path.join(chunk_dir, f"{i}.part")
                    with open(part_path, "rb") as part_f:
                        while data := part_f.read(CHUNK_SIZE):
                            out_f.write(data)
                            total_size += len(data)

        # Cleanup chunks
        shutil.rmtree(chunk_dir, ignore_errors=True)

        logger.info(
            f"[FILES] Assembled {safe_filename} for {body.agent_id} "
            f"({total_size} bytes, {body.total_chunks} chunks)"
        )

        return {
            "filename": safe_filename,
            "agent_id": body.agent_id,
            "size": total_size,
            "path": f"/api/files/{body.agent_id}/{safe_filename}",
        }
    except Exception as e:
        logger.error(f"[FILES] Assembly failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/files/{agent_id}", tags=["files"])
async def list_files(
    agent_id: str,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """List all uploaded files for an agent.

    Merges three sources so nothing already on record goes missing:
    input artifacts on S3 (this PR), the legacy ``uploads/{agent}/{file}``
    S3 key (still real data -- 454 objects on the dev bucket, unmigrated),
    and local disk (files that never made it to S3 -- David's counts: 214
    dev / 60 prod). This endpoint has no session_id, so the S3 input-artifact
    lookup is scoped to ``_shared`` -- the same scope an unsessioned upload
    writes to.
    """
    agent_id = _safe_agent_id(agent_id)
    await _validate_agent_ownership(agent_id, current_user)

    settings = get_settings()
    files_by_name: dict[str, dict] = {}

    if is_s3_artifact_storage_configured(settings):
        # Every session of this agent plus the legacy key, not just the
        # `_shared` scope: a file attached to a conversation belongs to that
        # conversation's scope and was missing from this list.
        for fname in await asyncio.to_thread(available_filenames, agent_id):
            files_by_name[fname] = {
                "filename": fname,
                "size": 0,
                "path": f"/api/files/{agent_id}/{fname}",
            }

    agent_dir = _contained_upload_path(agent_id)
    if os.path.exists(agent_dir):
        for fname in os.listdir(agent_dir):
            fpath = os.path.join(agent_dir, fname)
            if os.path.isfile(fpath) and fname not in files_by_name:
                files_by_name[fname] = {
                    "filename": fname,
                    "size": os.path.getsize(fpath),
                    "path": f"/api/files/{agent_id}/{fname}",
                }

    return {"files": sorted(files_by_name.values(), key=lambda f: f["filename"])}


def _downloadable_body(raw: bytes) -> bytes:
    """Unwraps the artifact envelope so a download returns the file itself.

    Generated artifacts are stored as ``{"filename", "language", "code"}``
    (see tool_save_code_artifact and create_downloadable_file). Serving that
    JSON verbatim would hand the user a wrapper instead of their report.
    Anything else -- a PDF, an upload -- passes through untouched.
    """
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return raw

    if isinstance(payload, dict) and "code" in payload and "filename" in payload:
        return str(payload["code"]).encode("utf-8")
    return raw


@router.get("/files/{agent_id}/{filename}", tags=["files"])
async def download_file(
    agent_id: str,
    filename: str,
    token: str = Query(None, description="Download token (alternative to Bearer auth)"),
    current_user: user_schemas.User | None = Depends(get_optional_user),
):
    """Download a file. Requires either Bearer auth + agent ownership, or a
    scoped download token bound to this `agent_id`/`filename`."""
    # Sanitize filename early so scoping comparison is against the normalized form
    safe_filename = os.path.basename(filename)

    authenticated = False
    if current_user is not None:
        # Bearer path: enforce agent ownership
        agent_id = _safe_agent_id(agent_id)
        await _validate_agent_ownership(agent_id, current_user)
        authenticated = True
    elif token:
        payload = verify_download_token(token)
        if not payload:
            raise HTTPException(status_code=401, detail="Invalid download token")
        # Token claims must match the requested resource exactly
        if payload.get("agent_id") != agent_id or payload.get("filename") != safe_filename:
            raise HTTPException(status_code=403, detail="Download token scope mismatch")
        authenticated = True

    if not authenticated:
        raise HTTPException(status_code=401, detail="Authentication required (Bearer or ?token=)")

    settings = get_settings()

    # This route has no session_id, and looking only at the `_shared` scope
    # missed two things at once: an upload filed under its own session, and
    # anything an agent generated (create_downloadable_file now writes an
    # output artifact). The lookup below scans the agent's artifact prefixes
    # instead, then falls back to the legacy `uploads/` key.
    if is_s3_artifact_storage_configured(settings):
        try:
            content_bytes = await asyncio.to_thread(
                read_file_bytes, agent_id, safe_filename
            )
        except Exception as e:
            logger.error(f"[FILES] S3 Download failed: {e}")
            raise HTTPException(status_code=500, detail="Failed to fetch from S3")

        if content_bytes is not None:
            return Response(
                content=_downloadable_body(content_bytes),
                media_type="application/octet-stream",
                headers={"Content-Disposition": f'attachment; filename="{safe_filename}"'},
            )

    file_path = _contained_upload_path(agent_id, safe_filename)
    if os.path.exists(file_path):
        return FileResponse(file_path, filename=safe_filename)

    raise HTTPException(status_code=404, detail="File not found")

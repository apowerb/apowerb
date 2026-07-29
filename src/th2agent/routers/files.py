import os
import shutil
import tempfile
from fastapi import APIRouter, HTTPException, Depends, UploadFile, File, Form, Query, Request
from fastapi.responses import FileResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from logging import getLogger
from pydantic import BaseModel

from th2agent.auth.dependencies import get_current_user, get_optional_user
from th2agent.users import schemas as user_schemas
from th2agent.helpers.security import generate_download_token, verify_download_token
from th2agent.configs.paths import uploads_dir
from th2agent.configs.settings import get_settings
from th2agent.helpers.ownership import validate_agent_ownership as _validate_agent_ownership

logger = getLogger(__name__)
router = APIRouter()

security = HTTPBearer(auto_error=False)

CHUNK_SIZE = 65536  # 64 KB


class UploadCompleteRequest(BaseModel):
    upload_id: str
    agent_id: str
    filename: str
    total_chunks: int


@router.post("/files/upload", tags=["files"])
async def upload_file(
    file: UploadFile = File(...),
    agent_id: str = Form(...),
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Upload a file for an agent to access."""
    await _validate_agent_ownership(agent_id, current_user)

    # Sanitize filename
    filename = os.path.basename(file.filename)
    if not filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    try:
        content = await file.read()
        total_size = len(content)
        settings = get_settings()

        if settings.storage_mode == "local":
            agent_dir = str(uploads_dir() / agent_id)
            os.makedirs(agent_dir, exist_ok=True)
            file_path = os.path.join(agent_dir, filename)
            with open(file_path, "wb") as f:
                f.write(content)
        else:
            from th2agent.storage.s3 import upload_bytes_to_s3
            s3_key = f"uploads/{agent_id}/{filename}"
            upload_bytes_to_s3(content, s3_key, content_type=file.content_type)

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
    await _validate_agent_ownership(agent_id, current_user)

    safe_filename = os.path.basename(filename)
    if not safe_filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    if chunk_index < 0 or chunk_index >= total_chunks:
        raise HTTPException(status_code=400, detail="Invalid chunk_index")

    chunk_dir = str(uploads_dir() / "_chunks" / upload_id)
    os.makedirs(chunk_dir, exist_ok=True)

    chunk_path = os.path.join(chunk_dir, f"{chunk_index}.part")

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
    await _validate_agent_ownership(body.agent_id, current_user)

    safe_filename = os.path.basename(body.filename)
    if not safe_filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    chunk_dir = str(uploads_dir() / "_chunks" / body.upload_id)

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

    # Assemble into final destination
    agent_dir = str(uploads_dir() / body.agent_id)
    os.makedirs(agent_dir, exist_ok=True)
    final_path = os.path.join(agent_dir, safe_filename)

    try:
        total_size = 0
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
    """List all uploaded files for an agent."""
    await _validate_agent_ownership(agent_id, current_user)

    settings = get_settings()

    if settings.storage_mode == "local":
        agent_dir = str(uploads_dir() / agent_id)
        if not os.path.exists(agent_dir):
            return {"files": []}

        files = []
        for fname in os.listdir(agent_dir):
            fpath = os.path.join(agent_dir, fname)
            if os.path.isfile(fpath):
                files.append({
                    "filename": fname,
                    "size": os.path.getsize(fpath),
                    "path": f"/api/files/{agent_id}/{fname}",
                })
        return {"files": files}
    else:
        from th2agent.storage.s3 import list_files_in_s3
        prefix = f"uploads/{agent_id}/"
        s3_keys = list_files_in_s3(prefix=prefix)
        files = []
        for key in s3_keys:
            fname = key.replace(prefix, "")
            if "/" not in fname:  # Ensure it's not a subfolder
                files.append({
                    "filename": fname,
                    "size": 0,  # S3 list doesn't return size easily without extra calls
                    "path": f"/api/files/{agent_id}/{fname}",
                })
        return {"files": files}


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

    if settings.storage_mode == "local":
        file_path = str(uploads_dir() / agent_id / safe_filename)
        if not os.path.exists(file_path):
            raise HTTPException(status_code=404, detail="File not found")
        return FileResponse(file_path, filename=safe_filename)
    else:
        from th2agent.storage.s3 import download_file_from_s3, file_exists_in_s3
        s3_key = f"uploads/{agent_id}/{safe_filename}"

        if not file_exists_in_s3(s3_key):
            raise HTTPException(status_code=404, detail="File not found in S3")

        try:
            content_bytes = download_file_from_s3(s3_key)
            return Response(content=content_bytes, media_type="application/octet-stream", headers={
                "Content-Disposition": f'attachment; filename="{safe_filename}"'
            })
        except Exception as e:
            logger.error(f"[FILES] S3 Download failed: {e}")
            raise HTTPException(status_code=500, detail="Failed to fetch from S3")

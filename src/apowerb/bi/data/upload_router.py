"""
bi/data/upload_router.py
------------------------
CSV file upload endpoint for the BI module.

Accepts a CSV file, validates it, extracts metadata (headers, row count),
stores it in S3 via the BI file storage layer, and returns a summary
including sample rows.
"""

from __future__ import annotations

import csv
import io
import uuid as _uuid
from logging import getLogger

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile, status
from pydantic import BaseModel

from apowerb.auth.dependencies import get_current_user
from apowerb.users import schemas as user_schemas
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from apowerb.artifacts.upload_mirror import mirror_as_input_artifact
from apowerb.bi.data._bi_storage import bi_artifact_app_name, save_file
from apowerb.bi.db_stores import DatabaseDataStore
from apowerb.tools_store.tools_helpers import list_user_tool_configs
from apowerb.helpers.database import get_db

logger = getLogger(__name__)

router = APIRouter(tags=["bi-upload"])


class GoogleDriveImportRequest(BaseModel):
    organization_id: str
    project_id: str = "thaink2"
    file_id: str
    file_name: str
    tool_config_id: str
    

_SEPARATOR_MAP = {
    "comma": ",",
    "semicolon": ";",
    "tab": "\t",
    "pipe": "|",
}


def _detect_separator(text: str) -> str:
    """Auto-detect CSV separator using csv.Sniffer, fallback to comma."""
    try:
        sample = text[:8192]
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        return dialect.delimiter
    except csv.Error:
        return ","


@router.post(
    "/bi/upload-csv",
    summary="Upload a CSV file for BI analysis",
    description=(
        "Accepts a CSV file, validates the content, parses headers and row count, "
        "stores the file in S3, and returns metadata including the first 5 rows "
        "as a sample. Separator is auto-detected or can be set explicitly via "
        "the 'separator' form field (comma, semicolon, tab, pipe, or auto)."
    ),
)
async def upload_csv(
    file: UploadFile,
    current_user: user_schemas.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    organization_id: str = Form(...),
    project_id: str = Form(default="thaink2"),
    separator: str = Form(default="auto"),
) -> dict:
    filename = file.filename or "upload.csv"

    if not filename.lower().endswith(".csv"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only CSV files are accepted. The file must have a .csv extension.",
        )

    organization_id = organization_id.strip()
    project_id = project_id.strip()

    if not organization_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="organization_id is required.",
        )

    if not project_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="project_id is required.",
        )

    raw_bytes = await file.read()
    if not raw_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded CSV file is empty.",
        )

    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw_bytes.decode("latin-1")

    if separator == "auto":
        delim = _detect_separator(text)
    else:
        delim = _SEPARATOR_MAP.get(separator, separator)
        if len(delim) != 1:
            delim = ","

    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    columns: list[str] = reader.fieldnames or []

    if not columns:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="CSV file has no headers or is empty.",
        )

    rows: list[dict] = list(reader)
    row_count = len(rows)
    sample_rows = rows[:5]

    file_id = str(_uuid.uuid4())
    content_type = file.content_type or "text/csv"

    s3_key = save_file(
        organization_id=organization_id,
        project_id=project_id,
        file_id=file_id,
        filename=filename,
        content=raw_bytes,
        content_type=content_type,
    )
    # Store metadata in the database
    data_store = DatabaseDataStore(db, owner=str(current_user.email))

    try:
        await data_store.save(
            file_id=file_id,
            name=filename,
            organization_id=organization_id,
            project_id=project_id,
            metadata={
                "s3_key": s3_key,
                "content_type": content_type,
                "extension": ".csv",
                "row_count": row_count,
                "columns": columns,
                "separator": delim,
                "uploaded_by": str(current_user.email),
            },
        )
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A data source with this name already exists in this organization/project.",
        )

    # Additive mirror into the artifact chain so the upload shows up in the
    # Artifacts tab (kind=input) -- bi/data storage above is unaffected and
    # stays the source csv_executor reads. Never fails the upload: caught
    # again here even though mirror_as_input_artifact already swallows its
    # own errors, per the "best-effort, log loud" rule this chain has broken
    # silently three times before.
    try:
        await mirror_as_input_artifact(
            app_name=bi_artifact_app_name(organization_id),
            session_id=None,
            filename=filename,
            data=raw_bytes,
            content_type=content_type,
            source="bi",
        )
    except Exception:
        logger.error(
            "[BI_UPLOAD] Artifact mirror raised for %r (org=%s)",
            filename, organization_id, exc_info=True,
        )

    return {
        "file_id": file_id,
        "filename": filename,
        "content_type": content_type,
        "organization_id": organization_id,
        "project_id": project_id,
        "key": s3_key,
        "columns": columns,
        "row_count": row_count,
        "sample_rows": sample_rows,
        "separator": delim,
        "uploaded_by": str(current_user.email),
    }


@router.post(
    "/bi/import/google-drive",
    summary="Import a Google Drive file for BI analysis",
)
async def import_google_drive(
    request: GoogleDriveImportRequest,
    current_user: user_schemas.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    internal_file_id = str(_uuid.uuid4())
    data_store = DatabaseDataStore(db, owner=str(current_user.email))

    await data_store.save(
        file_id=internal_file_id,
        name=request.file_name,
        organization_id=request.organization_id,
        project_id=request.project_id,
        metadata={
            "source_type": "google_drive",
            "source_options": {
                "file_id": request.file_id,
                "tool_config_id": request.tool_config_id
            },
            "extension": ".csv", # Default extension since we export sheets as CSV
            "uploaded_by": str(current_user.email),
            "content_type": "text/csv",
        },
    )

    return {
        "file_id": internal_file_id,
        "filename": request.file_name,
        "organization_id": request.organization_id,
        "project_id": request.project_id,
        "source_type": "google_drive"
    }


@router.get(
    "/bi/tool-configs/google-drive",
    summary="List user's connected Google Drive tool configurations",
)
async def list_gdrive_configs(
    current_user: user_schemas.User = Depends(get_current_user),
) -> list[dict]:
    # Fetch all user endpoints/tools
    configs = list_user_tool_configs(user_id=str(current_user.email))
    
    # Filter for tool configs relating to Google Drive
    gdrive_configs = [
        c for c in configs 
        if "google" in c.get("tool_name", "").lower() 
        or c.get("tool_name") == "gdrive"
    ]
    
    return gdrive_configs

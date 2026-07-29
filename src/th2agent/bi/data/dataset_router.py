"""
bi/data/dataset_router.py
--------------------------
Endpoints for listing uploaded BI datasets, previewing their content,
and listing database tool configurations for the BI module.

Metadata comes from DB via DatabaseDataStore.
Actual file bytes remain in S3.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
from datetime import datetime
from typing import Any

import httpx
from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from th2agent.auth.dependencies import get_current_user
from th2agent.bi.data._bi_storage import read_file
from th2agent.bi.db_stores import DatabaseDataStore
from th2agent.helpers.database import get_db
from th2agent.helpers.encryptor import decrypt_value
from th2agent.helpers.env_scope import env_scope
from th2agent.models import BusinessIntelligence, Integration, User
from th2agent.tools_store.portfolio.integration_status import IntegrationStatusError
from th2agent.tools_store.portfolio.onedrive_core import (
    _GRAPH_BASE,
    _graph_headers,
    download_and_parse_spreadsheet,
)
from th2agent.users import schemas as user_schemas

router = APIRouter(tags=["bi-datasets"])
logger = logging.getLogger(__name__)

# Serialise OneDrive token swaps per-process — mirrors the pattern used in
# ``routers/onedrive_browser.py`` and ``bi/data/onedrive_excel_executor.py``.
_onedrive_env_lock = asyncio.Lock()

_PREVIEW_SAMPLE_SIZE = 10


def _detect_column_type(values: list[str]) -> str:
    dominated_by_empty = True
    for v in values:
        if v is None or v.strip() == "":
            continue
        dominated_by_empty = False
        break
    if dominated_by_empty:
        return "string"

    for v in values:
        if v is None or v.strip() == "":
            continue
        try:
            int(v)
            continue
        except ValueError:
            break
    else:
        return "int"

    for v in values:
        if v is None or v.strip() == "":
            continue
        try:
            float(v)
            continue
        except ValueError:
            break
    else:
        return "float"

    date_formats = [
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S",
        "%Y/%m/%d",
        "%d/%m/%Y",
        "%m/%d/%Y",
    ]
    for fmt in date_formats:
        all_match = True
        for v in values:
            if v is None or v.strip() == "":
                continue
            try:
                datetime.strptime(v.strip(), fmt)
            except ValueError:
                all_match = False
                break
        if all_match:
            return "date"

    return "string"


def _detect_separator(text: str) -> str:
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
        return dialect.delimiter
    except csv.Error:
        return ","


def _row_to_dataset_item(row: BusinessIntelligence) -> dict[str, Any]:
    cfg = row.config or {}
    columns = cfg.get("columns", [])
    return {
        "file_id": row.id,
        "filename": row.name,
        "organization_id": row.organization_id,
        "project_id": row.project_id,
        "key": cfg.get("s3_key"),
        "content_type": cfg.get("content_type"),
        "extension": cfg.get("extension"),
        "columns_count": len(columns),
        "row_count": cfg.get("row_count", 0),
        "separator": cfg.get("separator"),
        "uploaded_by": cfg.get("uploaded_by", row.owner),
    }


async def _find_dataset_row(
    db: AsyncSession,
    owner: str,
    organization_id: str,
    project_id: str,
    file_id: str,
) -> BusinessIntelligence | None:
    store = DatabaseDataStore(db, owner=owner)
    return await store.get_scoped(
        file_id=file_id,
        organization_id=organization_id,
        project_id=project_id,
    )


@router.get("/bi/datasets", summary="List uploaded datasets")
async def list_datasets(
    current_user: user_schemas.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    organization_id: str = Query(...),
    project_id: str = Query(default="thaink2"),
    extension: str | None = Query(default=None),
) -> dict:
    store = DatabaseDataStore(db, owner=str(current_user.email))
    rows, _total = await store.list(
        page=1,
        page_size=1000,
        organization_id=organization_id,
        project_id=project_id,
    )

    datasets: list[dict[str, Any]] = []
    for row in rows:
        cfg = row.config or {}
        if extension and cfg.get("extension") != extension:
            continue
        datasets.append(_row_to_dataset_item(row))

    return {"datasets": datasets}


class OnedrivePreviewRequest(BaseModel):
    """Body for ``POST /api/v1/bi/onedrive/preview``.

    Either ``item_path`` or ``item_id`` must be provided. When both are
    set, ``item_path`` wins (no Graph round-trip needed). ``sheet_name``
    accepts either a string (sheet title) or an integer (0-based index)
    — ignored for ``.csv`` / ``.tsv`` files.
    """

    item_path: str | None = Field(default=None)
    item_id: str | None = Field(default=None)
    drive_id: str | None = Field(default=None)
    sheet_name: str | int | None = Field(default=None)


def _pandas_dtype_to_preview_type(series: "Any") -> str:
    """Map a pandas dtype to the same vocabulary used by the CSV preview."""
    import pandas as pd

    dtype = series.dtype
    if pd.api.types.is_bool_dtype(dtype):
        return "bool"
    if pd.api.types.is_integer_dtype(dtype):
        return "int"
    if pd.api.types.is_float_dtype(dtype):
        return "float"
    if pd.api.types.is_datetime64_any_dtype(dtype):
        return "date"
    return "string"


_SPREADSHEET_SHEET_EXTENSIONS = {".xlsx", ".xls", ".xlsm", ".ods"}


def _path_supports_sheet_name(item_path: str) -> bool:
    """Return True if the file extension can carry multiple sheets (xlsx/xls/xlsm/ods).

    For CSV/TSV the sheet_name field is meaningless and the response should
    expose ``sheet_name: null`` so the frontend hides the selector.
    """
    lowered = item_path.lower()
    return any(lowered.endswith(ext) for ext in _SPREADSHEET_SHEET_EXTENSIONS)


def _resolve_path_from_item_id(item_id: str, headers: dict[str, str]) -> str:
    """Resolve a Graph driveItem id to its path relative to the drive root.

    Mirrors ``OnedriveExcelQueryExecutor._resolve_path_from_id`` —
    duplicated rather than shared to avoid creating a new cross-module
    coupling for a single Graph call. Kept module-level so tests can
    monkeypatch it without touching the executor.
    """
    resp = httpx.get(
        f"{_GRAPH_BASE}/me/drive/items/{item_id}",
        headers=headers,
        timeout=20,
    )
    if resp.status_code == 404:
        raise RuntimeError(
            f"Graph metadata request for item_id={item_id} returned HTTP 404"
        )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Graph metadata request for item_id={item_id} returned HTTP {resp.status_code}"
        )
    meta = resp.json()
    name = meta.get("name")
    parent_ref = meta.get("parentReference") or {}
    parent_path = parent_ref.get("path") or ""
    prefix = "/drive/root:"
    if parent_path.startswith(prefix):
        parent_path = parent_path[len(prefix):]
    return f"{parent_path.strip('/')}/{name}".strip("/")


async def _resolve_onedrive_refresh_token(
    current_user: user_schemas.User,
    db: AsyncSession,
) -> str:
    """Return the decrypted OneDrive refresh_token or raise ``RuntimeError``.

    Mirrors ``routers.onedrive_browser._resolve_onedrive_refresh_token`` —
    kept local to avoid a cross-router import cycle.
    """
    result = await db.execute(
        select(Integration).where(
            Integration.user_id == current_user.user_id,
            Integration.provider == "microsoft_onedrive",
        )
    )
    integration = result.scalar_one_or_none()

    if not integration or not integration.refresh_token:
        raise RuntimeError(
            "OneDrive credentials are not configured. "
            "The user must connect their OneDrive account via the integrations settings page."
        )

    try:
        return decrypt_value(integration.refresh_token)
    except InvalidToken:
        logger.warning(
            "OneDrive refresh_token for user_id=%s is plaintext — "
            "run `python -m th2agent.cli.migrate_integrations --encrypt-legacy`.",
            current_user.user_id,
        )
        return integration.refresh_token


@router.post(
    "/bi/onedrive/preview",
    summary="Preview a OneDrive spreadsheet (xlsx/xls/xlsm/ods/csv/tsv)",
)
async def preview_onedrive_spreadsheet(
    payload: OnedrivePreviewRequest,
    current_user: user_schemas.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Download a OneDrive tabular file and return a CSV-preview-shaped
    response so the frontend wizard can share its rendering logic.

    Response body::

        {
          "columns":     [{"name": str, "type": str}, ...],
          "row_count":   int,
          "sample_rows": [ {col: value, ...}, ... ],  # ≤ 10 rows
          "sheet_name":  str | None  # echo of the active sheet (null for csv/tsv)
        }

    Error codes:
      - 400 — neither ``item_path`` nor ``item_id`` was provided
      - 401 — user has no OneDrive integration
      - 404 — file does not exist on the drive
      - 415 — file extension not supported
      - 500 — any other parse failure
    """
    item_path = (payload.item_path or "").strip()
    item_id = (payload.item_id or "").strip()

    if not item_path and not item_id:
        return JSONResponse(
            {
                "status": "error",
                "message": "Either 'item_path' or 'item_id' is required.",
            },
            status_code=400,
        )

    try:
        refresh_token = await _resolve_onedrive_refresh_token(current_user, db)
    except IntegrationStatusError as exc:
        return JSONResponse(
            {
                "status": "error",
                "message": exc.message,
                "code": exc.code,
                "provider": exc.provider,
                "remediable_by_reconnect": exc.is_remediable_by_reconnect,
            },
            status_code=exc.http_status_code,
        )
    except RuntimeError as exc:
        return JSONResponse(
            {"status": "error", "message": str(exc)},
            status_code=401,
        )

    try:
        async with env_scope(
            {"ONEDRIVE_REFRESH_TOKEN": refresh_token},
            lock=_onedrive_env_lock,
        ):
            headers = _graph_headers()
    except IntegrationStatusError as exc:
        return JSONResponse(
            {
                "status": "error",
                "message": exc.message,
                "code": exc.code,
                "provider": exc.provider,
                "remediable_by_reconnect": exc.is_remediable_by_reconnect,
            },
            status_code=exc.http_status_code,
        )
    except RuntimeError as exc:
        return JSONResponse(
            {"status": "error", "message": str(exc)},
            status_code=401,
        )

    # Resolve item_id → item_path when item_path is not provided directly.
    if not item_path:
        try:
            item_path = await asyncio.to_thread(
                _resolve_path_from_item_id, item_id, headers
            )
        except IntegrationStatusError as exc:
            return JSONResponse(
                {
                    "status": "error",
                    "message": exc.message,
                    "code": exc.code,
                    "provider": exc.provider,
                    "remediable_by_reconnect": exc.is_remediable_by_reconnect,
                },
                status_code=exc.http_status_code,
            )
        except RuntimeError as exc:
            msg = str(exc)
            code = 404 if "404" in msg else 500
            return JSONResponse(
                {"status": "error", "message": msg},
                status_code=code,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("OneDrive item_id resolution crashed for %s", item_id)
            return JSONResponse(
                {
                    "status": "error",
                    "message": f"Failed to resolve item_id '{item_id}': {exc}",
                },
                status_code=500,
            )

    # Normalise sheet_name: accept int or str (digit strings → 0-based index).
    sheet_arg: str | int | None
    raw_sheet = payload.sheet_name
    if isinstance(raw_sheet, int):
        sheet_arg = raw_sheet
    elif isinstance(raw_sheet, str):
        stripped = raw_sheet.strip()
        if not stripped:
            sheet_arg = None
        elif stripped.isdigit():
            sheet_arg = int(stripped)
        else:
            sheet_arg = stripped
    else:
        sheet_arg = None

    try:
        df, err = await asyncio.to_thread(
            download_and_parse_spreadsheet,
            item_path,
            headers,
            sheet_name=sheet_arg,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "OneDrive preview parser crashed for %s", item_path
        )
        return JSONResponse(
            {
                "status": "error",
                "message": f"Failed to read OneDrive file: {exc}",
            },
            status_code=500,
        )

    if err is not None or df is None:
        msg = err or "Unable to parse the OneDrive file."
        msg_lower = msg.lower()
        if "unsupported" in msg_lower:
            code = 415
        elif "404" in msg:
            code = 404
        else:
            code = 500
        return JSONResponse(
            {"status": "error", "message": msg},
            status_code=code,
        )

    columns: list[dict[str, str]] = [
        {"name": str(col), "type": _pandas_dtype_to_preview_type(df[col])}
        for col in df.columns
    ]
    row_count = int(len(df))
    import pandas as pd  # local import — same pattern as the executor
    from th2agent.helpers.jsonify import to_jsonable
    clean_sample = (
        df.head(_PREVIEW_SAMPLE_SIZE)
        .astype(object)
        .where(pd.notna(df.head(_PREVIEW_SAMPLE_SIZE)), None)
    )
    sample_rows = [to_jsonable(r) for r in clean_sample.to_dict(orient="records")]

    # Echo the active sheet so the frontend can keep the selector in sync.
    # CSV/TSV files have no sheet concept → always None.
    response_sheet: str | None
    if not _path_supports_sheet_name(item_path):
        response_sheet = None
    elif sheet_arg is None:
        response_sheet = None
    else:
        response_sheet = str(sheet_arg)

    return JSONResponse(
        {
            "columns": columns,
            "row_count": row_count,
            "sample_rows": sample_rows,
            "sheet_name": response_sheet,
        },
        status_code=200,
    )


@router.get(
    "/bi/datasets/{file_id}/preview",
    summary="Preview a dataset",
)
async def preview_dataset(
    file_id: str,
    current_user: user_schemas.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    organization_id: str = Query(...),
    project_id: str = Query(default="thaink2"),
) -> dict:
    row = await _find_dataset_row(
        db=db,
        owner=str(current_user.email),
        organization_id=organization_id,
        project_id=project_id,
        file_id=file_id,
    )
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"CSV file not found for id {file_id}",
        )

    cfg = row.config or {}
    s3_key = cfg.get("s3_key")
    if not s3_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Missing S3 key for dataset {file_id}",
        )

    csv_bytes = read_file(s3_key)
    if csv_bytes is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Dataset not found for id {file_id}",
        )

    text = csv_bytes.decode("utf-8-sig", errors="replace")
    detected_sep = _detect_separator(text)

    reader = csv.DictReader(io.StringIO(text), delimiter=detected_sep)
    headers = reader.fieldnames or []

    sample_rows: list[dict[str, str]] = []
    row_count = 0
    for raw_row in reader:
        row_count += 1
        if len(sample_rows) < 20:
            sample_rows.append(dict(raw_row))

    columns: list[dict[str, str]] = []
    for col_name in headers:
        sample_values = [row.get(col_name, "") for row in sample_rows]
        col_type = _detect_column_type(sample_values)
        columns.append({"name": col_name, "type": col_type})

    sep_label = {
        ",": "comma",
        ";": "semicolon",
        "\t": "tab",
        "|": "pipe",
    }.get(detected_sep, "comma")

    return {
        "file_id": row.id,
        "filename": row.name,
        "organization_id": row.organization_id,
        "project_id": row.project_id,
        "key": s3_key,
        "columns": columns,
        "row_count": row_count,
        "sample_rows": sample_rows,
        "separator": sep_label,
    }


@router.delete(
    "/bi/datasets/{file_id}",
    summary="Delete an uploaded dataset",
)
async def delete_dataset(
    file_id: str,
    current_user: user_schemas.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    organization_id: str = Query(...),
    project_id: str = Query(default="thaink2"),
) -> dict:
    row = await _find_dataset_row(
        db=db,
        owner=str(current_user.email),
        organization_id=organization_id,
        project_id=project_id,
        file_id=file_id,
    )
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Dataset not found for id {file_id}",
        )

    s3_key = (row.config or {}).get("s3_key")

    # store.delete() handles both S3 cleanup and soft-delete in DB
    store = DatabaseDataStore(db, owner=str(current_user.email))
    await store.delete(file_id)

    return {
        "status": "deleted",
        "file_id": file_id,
        "organization_id": organization_id,
        "project_id": project_id,
        "key": s3_key,
    }


@router.get(
    "/bi/tool-configs/database",
    summary="List database tool configurations",
)
async def list_database_tool_configs(
    current_user: user_schemas.User = Depends(get_current_user),
) -> dict:
    from th2agent.tools_store.tools_helpers import list_user_tool_configs

    all_configs = list_user_tool_configs(user_id=current_user.email)

    db_configs: list[dict[str, Any]] = []
    for cfg in all_configs:
        params = cfg.get("tool_config_params", {})
        if not isinstance(params, dict):
            continue
        has_db_fields = (
            ("host" in params or "db_host" in params)
            and ("port" in params or "db_port" in params)
            and ("database" in params or "db_database" in params)
        )
        if not has_db_fields:
            continue

        db_configs.append(
            {
                "id": cfg.get("tool_config_id", ""),
                "config_name": cfg.get("tool_config_name", ""),
                "db_type": params.get("db_type", params.get("type", "postgres")),
                "host": params.get("host", params.get("db_host", "")),
                "port": params.get("port", params.get("db_port", "")),
                "database": params.get("database", params.get("db_database", "")),
            }
        )

    return {"configs": db_configs}

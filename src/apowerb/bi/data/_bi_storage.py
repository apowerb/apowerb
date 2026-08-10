"""Unified BI file storage — S3 only.

Stores heavy BI files (CSV, Excel, JSON, etc.) in S3 under:
    bi/data/{organization_id}/{project_id}/data/{file_id}.{ext}
"""

from __future__ import annotations

import os
import re
from logging import getLogger
from typing import Any

from apowerb.storage.s3 import (
    upload_bytes_to_s3,
    download_file_from_s3,
    list_files_in_s3,
    delete_file_from_s3,
)

logger = getLogger(__name__)

_S3_PREFIX = "bi/data"


def _safe_segment(value: str) -> str:
    value = value.strip()
    value = value.replace("/", "-").replace("\\", "-")
    value = re.sub(r"\s+", "-", value)
    return value or "default"


def _get_extension(filename: str) -> str:
    ext = os.path.splitext(os.path.basename(filename))[1].lower()
    return ext if ext else ".bin"


def bi_artifact_app_name(organization_id: str) -> str:
    """Pseudo-agent folder for mirroring a BI upload into the artifact chain.

    BI has no agent concept -- uploads are scoped by organization_id/
    project_id only (see upload_router.py), unlike RAG where agent_id is a
    real, owned ``agent<digits>`` folder. The "bi-" prefix keeps this out of
    that namespace so it can never be mistaken for one (see
    apowerb.helpers.ownership / routers.rag.validators, both anchored on
    ``^agent\\d+$``).
    """
    return f"bi-{_safe_segment(organization_id)}"


def build_file_key(
    organization_id: str,
    project_id: str,
    file_id: str,
    filename: str,
) -> str:
    org = _safe_segment(organization_id)
    project = _safe_segment(project_id)
    ext = _get_extension(filename)
    return f"{_S3_PREFIX}/{org}/{project}/data/{file_id}{ext}"


def save_file(
    organization_id: str,
    project_id: str,
    file_id: str,
    filename: str,
    content: bytes,
    content_type: str,
) -> str:
    key = build_file_key(organization_id, project_id, file_id, filename)
    upload_bytes_to_s3(content, key, content_type)
    logger.info("[BI Storage] Saved file to S3: %s", key)
    return key


def read_file(key: str) -> bytes | None:
    try:
        return download_file_from_s3(key)
    except Exception as exc:
        logger.error("[BI Storage] Failed to download %s: %s", key, exc)
        return None


def delete_file(key: str) -> bool:
    try:
        delete_file_from_s3(key)
        logger.info("[BI Storage] Deleted file from S3: %s", key)
        return True
    except Exception as exc:
        logger.error("[BI Storage] Failed to delete %s: %s", key, exc)
        return False


def list_files(organization_id: str, project_id: str) -> list[dict[str, Any]]:
    org = _safe_segment(organization_id)
    project = _safe_segment(project_id)
    prefix = f"{_S3_PREFIX}/{org}/{project}/data/"

    try:
        keys = list_files_in_s3(prefix=prefix)
    except Exception as exc:
        logger.error("[BI Storage] Failed to list S3 keys for %s: %s", prefix, exc)
        return []

    results: list[dict[str, Any]] = []
    for key in keys:
        fname = key.rsplit("/", 1)[-1]
        file_id, ext = os.path.splitext(fname)
        results.append(
            {
                "file_id": file_id,
                "filename": fname,
                "organization_id": org,
                "project_id": project,
                "key": key,
                "extension": ext,
            }
        )
    return results

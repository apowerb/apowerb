"""Mirrors an uploaded file into the artifact chain, best-effort.

``/bi/upload-csv`` and ``/rag/index-files`` each keep their own storage --
BI: S3 under ``bi/data/...`` (``apowerb.bi.data._bi_storage``), RAG: local
disk under ``uploads_dir()`` (``apowerb.routers.rag.index_files``) -- and
that storage stays exactly where it is: it is what BI queries run against
and what the RAG indexer reads. This module adds a second, additional write
into the same key scheme ``routers/files.py`` already uses
(``apowerb.artifacts.s3_artifact_service``), so the upload also shows up in
the Artifacts tab with ``kind=input``.

Best-effort by design: the artifact copy is a visibility feature, not the
record of truth BI/RAG depend on. A failure here must never fail the upload
it mirrors, and must never fail silently either -- this exact storage chain
has already caused three silent outages.
"""

from __future__ import annotations

from logging import getLogger

from apowerb.artifacts.input_scope import resolve_input_session_id
from apowerb.artifacts.s3_artifact_service import S3ArtifactService
from apowerb.configs.artifact_service_config import is_s3_artifact_storage_configured
from apowerb.configs.settings import get_settings

logger = getLogger(__name__)


async def mirror_as_input_artifact(
    *,
    app_name: str,
    session_id: str | None,
    filename: str,
    data: bytes,
    content_type: str | None,
    source: str,
) -> None:
    """Writes ``data`` as an input artifact for ``(app_name, session_id)``.

    Never raises: callers keep serving their own upload response regardless
    of this call's outcome.
    """
    if not is_s3_artifact_storage_configured(get_settings()):
        # Silence here reads as "the feature is broken" to whoever uploaded
        # a file and cannot find it. Say it once, at debug level.
        logger.debug(
            "[ARTIFACT_MIRROR] S3 artifact storage is not configured; %s "
            "upload %r stays out of the Artifacts tab",
            source, filename,
        )
        return

    resolved_session_id = resolve_input_session_id(session_id)
    try:
        await S3ArtifactService().save_input_artifact(
            app_name=app_name,
            session_id=resolved_session_id,
            filename=filename,
            data=data,
            content_type=content_type,
        )
    except Exception:
        logger.error(
            "[ARTIFACT_MIRROR] Failed to mirror %s upload %r as an input "
            "artifact (app_name=%s, session_id=%s)",
            source, filename, app_name, resolved_session_id,
            exc_info=True,
        )

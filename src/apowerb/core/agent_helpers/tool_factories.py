"""Agent-bound tool factories: downloadable files, upload, and text-to-SQL rebinding.

``_make_read_uploaded_file`` lives in ``read_file_tool`` and ``_make_ingest_file``
lives in ``ingest_file_tool`` — they are re-exported here for backwards
compatibility.
"""
from __future__ import annotations

import os

from apowerb.configs.th2logger import setup_logging
from apowerb.configs.paths import uploads_dir
from apowerb.configs.settings import get_settings
from apowerb.storage.s3 import (
    upload_bytes_to_s3,
    upload_file_to_s3,
)
from apowerb.core.agent_helpers.pdf_writer import _write_pdf
from apowerb.core.agent_helpers.read_file_tool import _make_read_uploaded_file
from apowerb.core.agent_helpers.ingest_file_tool import _make_ingest_file
from apowerb.core.agent_helpers.pdf_to_images_tool import _make_pdf_to_images
from apowerb.scheduler.backlog_status_tool import _make_get_backlog_status


__all__ = [
    "_TEXT_TO_SQL_TOOL_NAMES",
    "_make_create_downloadable_file",
    "_make_upload_file",
    "_make_ingest_file",
    "_make_read_uploaded_file",
    "_make_pdf_to_images",
    "_make_get_backlog_status",
    "_resolve_text_to_sql_tools",
    "logger",
]


logger = setup_logging(__name__)


_TEXT_TO_SQL_TOOL_NAMES = {
    "tool_text_to_sql",
    "tool_get_database_schema",
    "tool_text_to_sql_explain",
    "tool_business_analyst",
}


def _make_create_downloadable_file(folder_name: str):
    """Create a create_downloadable_file tool bound to the agent's upload folder."""

    def create_downloadable_file(filename: str, content: str) -> dict:
        """Create a file that the user can download.
        Use this tool when the user asks you to generate a file, export data, create a report, etc.

        Choose the file extension based on the content format:
        - .pdf — for professional reports, documents to print or share (generates a real PDF)
        - .md  — for reports, summaries, formatted text (DEFAULT for non-PDF)
        - .csv — for tabular data
        - .json — for structured data
        - .html — for rich formatted documents with styling
        - .py / .js / .sql — for code
        - .txt — only for truly plain unformatted text

        For .pdf files: write the content using markdown formatting (# headings, **bold**, - bullets).
        The content will be automatically converted to a properly formatted PDF document.

        If the filename has no extension, ".md" is added automatically.

        Args:
            filename: The name for the file with appropriate extension (e.g. "rapport.pdf", "synthese.md", "data.csv").
            content: The text content to write into the file. For PDF, use markdown-style formatting.

        Returns:
            A dict with the download path or an error message.
        """
        settings=get_settings()
        import os
        import re
        safe_name = os.path.basename(filename)
        safe_name = re.sub(r'[<>:"/\\|?*]', "_", safe_name)

        if not safe_name:
            return {"status": "error", "message": "Invalid filename"}

        if not os.path.splitext(safe_name)[1]:
            safe_name += ".md"

        ### if we want to use it locally
        if settings.storage_mode=="local":
            import os
            import re

            # Sanitize filename
            safe_name = os.path.basename(filename)
            safe_name = re.sub(r'[<>:"/\\|?*]', "_", safe_name)
            if not safe_name:
                return {"status": "error", "message": "Invalid filename"}

            # Default to .md if no extension provided
            if not os.path.splitext(safe_name)[1]:
                safe_name += ".md"

            agent_dir = str(uploads_dir() / folder_name)
            os.makedirs(agent_dir, exist_ok=True)
            file_path = os.path.join(agent_dir, safe_name)

            try:
                ext = os.path.splitext(safe_name)[1].lower()
                if ext == ".pdf":
                    _write_pdf(file_path, content)
                else:
                    with open(file_path, "w", encoding="utf-8") as f:
                        f.write(content)

                size = os.path.getsize(file_path)
                download_path = f"/api/files/{folder_name}/{safe_name}"
                return {
                    "status": "success",
                    "filename": safe_name,
                    "size": size,
                    "download_path": download_path,
                    "message": f"File created. The user can download it at: {download_path}",
                }
            except Exception as e:
                return {"status": "error", "message": f"Failed to create file: {e}"}
        else:
            logger.info(f"STORAGE MODE = {settings.storage_mode}")
            logger.info("USING S3 NOW in create_downloadable_file")
            import tempfile
            s3_key = f"uploads/{folder_name}/{safe_name}"

            try:
                ext = os.path.splitext(safe_name)[1].lower()

                if ext == ".pdf":
                    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                        tmp_path = tmp.name

                    try:
                        _write_pdf(tmp_path, content)
                        size = os.path.getsize(tmp_path)
                        upload_file_to_s3(tmp_path, s3_key)
                    finally:
                        if os.path.exists(tmp_path):
                            os.remove(tmp_path)
                else:
                    content_bytes = content.encode("utf-8")
                    size = len(content_bytes)
                    upload_bytes_to_s3(
                        content_bytes,
                        s3_key,
                        content_type="text/plain; charset=utf-8",
                    )

                download_path = f"/api/files/{folder_name}/{safe_name}"
                return {
                    "status": "success",
                    "filename": safe_name,
                    "size": size,
                    "download_path": download_path,
                    "message": f"File created. The user can download it at: {download_path}",
                }
            except Exception as e:
                return {"status": "error", "message": f"Failed to create file: {e}"}


    return create_downloadable_file


def _make_upload_file(folder_name: str):
    """Bind tool_upload_file to the agent's uploads folder so the agent passes only the filename."""

    def tool_upload_file(
        filename: str,
        destination_path: str | None = None,
        destination_folder_id: str | None = None,
        conflict_behavior: str = "rename",
    ) -> dict:
        """Upload a file created by this agent to OneDrive.

        Pass the filename returned by create_downloadable_file.
        The local path is resolved automatically.

        Args:
            filename:              Name of the file to upload (e.g. "report.csv").
            destination_path:      OneDrive path including filename, e.g. "Documents/report.csv".
                                   Uploads to drive root if omitted.
            destination_folder_id: Folder item ID from tool_list_files or tool_create_folder.
                                   Ignored if destination_path is set.
            conflict_behavior:     "rename" (default) | "replace" | "fail".

        Returns:
            dict with keys: status, id, name, size_bytes, webUrl, parentPath.
        """
        import os
        from apowerb.tools_store.portfolio.onedrive import tool_upload_file as _upload

        local_path = str(uploads_dir() / folder_name / filename)
        if not os.path.exists(local_path):
            agent_dir = str(uploads_dir() / folder_name)
            available = [f for f in os.listdir(agent_dir) if os.path.isfile(os.path.join(agent_dir, f))] if os.path.exists(agent_dir) else []
            return {"status": "error", "message": f"File '{filename}' not found.", "available_files": available, "retry": False}

        return _upload(local_file_path=local_path, destination_path=destination_path,
                       destination_folder_id=destination_folder_id, conflict_behavior=conflict_behavior)

    return tool_upload_file


def _resolve_text_to_sql_tools(
    agent_name: str, tools_ids: list, tools_funcs: list, owner_id: str
) -> list:
    """Remove unbound text-to-SQL placeholders and replace with agent-bound versions,
    resolving the DB config from the agent's configured tool params.

    ``owner_id`` scopes the tool_config lookup so an agent cannot pull DB
    credentials from a foreign tenant's config.
    """
    from apowerb.tools_store.portfolio.text_to_sql import make_text_to_sql_tools
    from apowerb.tools_store.tools_helpers import (
        load_tool_config_params,
        normalize_db_params,
    )

    tools_funcs = [
        fn
        for fn in tools_funcs
        if getattr(fn, "__name__", "") not in _TEXT_TO_SQL_TOOL_NAMES
    ]

    db_params = None
    logger.info(f"[TO_AGENT] Searching DB config in tools_ids: {tools_ids}")
    for tid in tools_ids:
        try:
            _, tparams = load_tool_config_params(tid, owner_id=owner_id)
            tparams = normalize_db_params(tparams)  # accept lowercase host/database/...
            logger.info(
                f"[TO_AGENT] tool {tid!r} params keys: {list(tparams.keys()) if tparams else None}"
            )
            if tparams and tparams.get("DB_NAME"):
                db_params = tparams
                logger.info(
                    f"[TO_AGENT] Found DB config in tool {tid!r}: DB={tparams.get('DB_NAME')!r}"
                )
                break
        except Exception as e:
            logger.info(f"[TO_AGENT] tool {tid!r} is not a DB config (skipping): {e}")

    tools_funcs.extend(make_text_to_sql_tools(agent_name, db_params=db_params))
    logger.info(
        "[TO_AGENT] text-to-SQL tools rebound for %s (%s)",
        agent_name,
        f"DB={db_params['DB_NAME']!r}" if db_params else "no DB configured",
    )
    return tools_funcs

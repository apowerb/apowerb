"""Agent-bound ``read_uploaded_file`` tool factory.

Extracted from ``tool_factories`` to keep module sizes manageable.
"""
from __future__ import annotations

import os

from th2agent.configs.th2logger import setup_logging
from th2agent.configs.paths import uploads_dir
from th2agent.configs.settings import get_settings
from th2agent.storage.s3 import (
    download_file_from_s3,
    file_exists_in_s3,
    list_files_in_s3,
)
from th2agent.core.agent_helpers.text_utils import _truncate_content
from th2agent.core.agent_helpers.pdf_writer import _extract_pdf_text
from th2agent.core.agent_helpers.file_readers import (
    _IMAGE_EXTENSIONS,
    _AUDIO_EXTENSIONS,
    _ARCHIVE_EXTENSIONS,
    _is_binary_by_magic,
    _read_image,
    _read_archive,
    _read_excel,
    _read_docx,
    _read_pptx,
    _read_binary_metadata,
)


logger = setup_logging(__name__)


def _make_read_uploaded_file(folder_name: str):
    """Create a read_uploaded_file tool with the agent folder name pre-bound."""

    def read_uploaded_file(filename: str) -> dict:
        """Read a file that was uploaded by the user for this agent.
        Use this tool when the user mentions a file they uploaded or when the message indicates files were uploaded.

        Args:
            filename: The name of the uploaded file to read.

        Returns:
            A dict with the file content or an error message.
        """
        settings=get_settings()
        if settings.storage_mode=="local":
            import os
            file_path = str(uploads_dir() / folder_name / filename)

            if not os.path.exists(file_path):
                agent_dir = str(uploads_dir() / folder_name)
                available = []
                if os.path.exists(agent_dir):
                    available = [
                        f
                        for f in os.listdir(agent_dir)
                        if os.path.isfile(os.path.join(agent_dir, f))
                    ]
                return {
                    "status": "error",
                    "message": f"File '{filename}' not found in {folder_name}",
                    "available_files": available,
                }

            file_size = os.path.getsize(file_path)
            if file_size > 10 * 1024 * 1024:
                return {
                    "status": "error",
                    "message": f"File too large ({file_size} bytes, max 10 MB)",
                }

            ext = os.path.splitext(filename)[1].lower()

            # PDF: extract text instead of reading raw bytes
            if ext == ".pdf":
                text = _extract_pdf_text(file_path)
                if text:
                    content, truncated = _truncate_content(text)
                    return {
                        "status": "success",
                        "filename": filename,
                        "content": content,
                        "size": file_size,
                        "format": "pdf_extracted_text",
                        "truncated": truncated,
                    }
                return {
                    "status": "error",
                    "filename": filename,
                    "size": file_size,
                    "message": "PDF file contains no extractable text (may be scanned/image-based)",
                }

            # Images: return metadata + base64 (if small enough)
            if ext in _IMAGE_EXTENSIONS:
                return _read_image(file_path, file_size, filename)

            # Audio files: return file path so audio tools can process them
            if ext in _AUDIO_EXTENSIONS:
                abs_path = os.path.abspath(file_path)
                return {
                    "status": "success",
                    "filename": filename,
                    "file_path": abs_path,
                    "size": file_size,
                    "format": "audio",
                    "content_type": "audio/" + ext.lstrip(".").replace("mp3", "mpeg").replace("m4a", "mp4"),
                    "message": (
                        f"Audio file ({ext}, {file_size} bytes) available at: {abs_path}\n"
                        "Use tool_speech_to_text or tool_analyze_audio with this file_path to process it."
                    ),
                }

            # ZIP archives: list contents
            if ext in _ARCHIVE_EXTENSIONS:
                return _read_archive(file_path, file_size, filename)
                        # Excel spreadsheets
            if ext in {".xlsx", ".xls"}:
                return _read_excel(file_path, file_size, filename)

            # Word documents
            if ext in {".docx", ".doc"}:
                return _read_docx(file_path, file_size, filename)

            # PowerPoint presentations
            if ext in {".pptx", ".ppt"}:
                return _read_pptx(file_path, file_size, filename)

            # Other binary detected by magic bytes: return metadata
            if _is_binary_by_magic(file_path):
                return _read_binary_metadata(file_path, file_size, filename)

            # Text files: try UTF-8, then cp1252 (no latin-1 — it accepts anything including binary)
            for encoding in ["utf-8", "cp1252"]:
                try:
                    with open(file_path, "r", encoding=encoding) as f:
                        content = f.read()
                    content, truncated = _truncate_content(content)
                    return {
                        "status": "success",
                        "filename": filename,
                        "content": content,
                        "size": file_size,
                        "encoding": encoding,
                        "truncated": truncated,
                    }
                except (UnicodeDecodeError, ValueError):
                    continue

            return {
                "status": "binary",
                "filename": filename,
                "size": file_size,
                "message": "Could not decode file as text (not UTF-8 or CP1252)",
            }
        else:
            logger.info("==================================================USING S3 NOW in _make_read_uploaded_file\n ===================================================\n========================================\n==================\n===================")
            import os
            import tempfile
            s3_key = f"uploads/{folder_name}/{filename}"
            prefix = f"uploads/{folder_name}/"

            if not file_exists_in_s3(s3_key):
                available = []
                for key in list_files_in_s3(prefix):
                    short_name = key.removeprefix(prefix)
                    if short_name and "/" not in short_name:
                        available.append(short_name)

                return {
                    "status": "error",
                    "message": f"File '{filename}' not found in {folder_name}",
                    "available_files": available,
                }

            content_bytes = download_file_from_s3(s3_key)
            file_size = len(content_bytes)

            if file_size > 10 * 1024 * 1024:
                return {
                    "status": "error",
                    "message": f"File too large ({file_size} bytes, max 10 MB)",
                }

            ext = os.path.splitext(filename)[1].lower()

            if ext == ".pdf":
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                    tmp.write(content_bytes)
                    tmp_path = tmp.name

                try:
                    text = _extract_pdf_text(tmp_path)
                finally:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)

                if text:
                    content, truncated = _truncate_content(text)
                    return {
                        "status": "success",
                        "filename": filename,
                        "content": content,
                        "size": file_size,
                        "format": "pdf_extracted_text",
                        "truncated": truncated,
                    }

                return {
                    "status": "error",
                    "filename": filename,
                    "size": file_size,
                    "message": "PDF file contains no extractable text (may be scanned/image-based)",
                }

            if ext in _IMAGE_EXTENSIONS:
                suffix = ext or ".bin"
                with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                    tmp.write(content_bytes)
                    tmp_path = tmp.name

                try:
                    return _read_image(tmp_path, file_size, filename)
                finally:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)

            # Audio files: save to temp and return path for audio tools
            if ext in _AUDIO_EXTENSIONS:
                suffix = ext or ".mp3"
                with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                    tmp.write(content_bytes)
                    tmp_path = tmp.name

                return {
                    "status": "success",
                    "filename": filename,
                    "file_path": tmp_path,
                    "size": file_size,
                    "format": "audio",
                    "content_type": "audio/" + ext.lstrip(".").replace("mp3", "mpeg").replace("m4a", "mp4"),
                    "message": (
                        f"Audio file ({ext}, {file_size} bytes) available at: {tmp_path}\n"
                        "Use tool_speech_to_text or tool_analyze_audio with this file_path to process it."
                    ),
                }

            if ext in _ARCHIVE_EXTENSIONS:
                suffix = ext or ".zip"
                with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                    tmp.write(content_bytes)
                    tmp_path = tmp.name

                try:
                    return _read_archive(tmp_path, file_size, filename)
                finally:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
            ########
            if ext in {".xlsx", ".xls", ".docx", ".doc", ".pptx", ".ppt"}:
                suffix = ext
                with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                    tmp.write(content_bytes)
                    tmp_path = tmp.name

                try:
                    if ext in {".xlsx", ".xls"}:
                        return _read_excel(tmp_path, file_size, filename)
                    if ext in {".docx", ".doc"}:
                        return _read_docx(tmp_path, file_size, filename)
                    if ext in {".pptx", ".ppt"}:
                        return _read_pptx(tmp_path, file_size, filename)
                finally:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)

            suffix = ext or ".bin"
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp.write(content_bytes)
                tmp_path = tmp.name

            try:
                if _is_binary_by_magic(tmp_path):
                    return _read_binary_metadata(tmp_path, file_size, filename)
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)

            for encoding in ["utf-8", "cp1252"]:
                try:
                    content = content_bytes.decode(encoding)
                    content, truncated = _truncate_content(content)
                    return {
                        "status": "success",
                        "filename": filename,
                        "content": content,
                        "size": file_size,
                        "encoding": encoding,
                        "truncated": truncated,
                    }
                except UnicodeDecodeError:
                    continue

            return {
                "status": "binary",
                "filename": filename,
                "size": file_size,
                "message": "Could not decode file as text (not UTF-8 or CP1252)",
            }

    return read_uploaded_file

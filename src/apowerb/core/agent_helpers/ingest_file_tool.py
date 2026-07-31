"""Agent-bound ``ingest_file`` tool factory for RAG agents.

Extracted from ``tool_factories`` to keep module sizes manageable.
"""
from __future__ import annotations

import os

from apowerb.configs.th2logger import setup_logging
from apowerb.configs.paths import uploads_dir
from apowerb.configs.settings import get_settings
from apowerb.storage.s3 import (
    download_file_from_s3,
    file_exists_in_s3,
    list_files_in_s3,
)
from apowerb.core.agent_helpers.pdf_writer import _extract_pdf_text


logger = setup_logging(__name__)


def _make_ingest_file(folder_name: str):
    """Create an ingest_file tool for RAG agents — reads, creates corpus, indexes. Never returns raw content."""

    def ingest_file(filename: str, corpus_name: str = "") -> dict:
        """Ingest an uploaded file into the RAG knowledge base. This reads the file,
        creates a corpus if needed, and indexes the content. Use this tool FIRST when a user uploads a file.

        Args:
            filename: The name of the uploaded file to ingest.
            corpus_name: Optional name for the corpus. Defaults to the filename.

        Returns:
            dict with corpus_id, indexed document count, and status. Does NOT return file content.
        """
        settings = get_settings()

        import os
        from apowerb.tools_store.portfolio.rag import (
            tool_create_corpus,
            tool_index_documents,
            tool_list_corpora,
        )
        if settings.storage_mode=="local":

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
                    "message": f"File '{filename}' not found",
                    "available_files": available,
                }

            # Read file content
            ext = os.path.splitext(filename)[1].lower()
            content = None

            if ext == ".pdf":
                content = _extract_pdf_text(file_path)
            else:
                for encoding in ["utf-8", "cp1252"]:
                    try:
                        with open(file_path, "r", encoding=encoding) as f:
                            content = f.read()
                        break
                    except (UnicodeDecodeError, ValueError):
                        continue

            if not content:
                return {
                    "status": "error",
                    "message": f"Could not extract text from '{filename}'",
                }

            # Check for existing corpus with same name
            name = corpus_name or filename
            existing = tool_list_corpora()
            corpus_id = None
            for c in existing.get("corpora", []):
                if c["name"] == name:
                    corpus_id = c["corpus_id"]
                    break

            # Create corpus if needed
            if not corpus_id:
                result = tool_create_corpus(
                    name=name, description=f"Indexed from uploaded file: {filename}"
                )
                corpus_id = result["corpus_id"]

            # Split content into chunks for indexing
            chunk_size = 1000
            chunks = [
                content[i : i + chunk_size] for i in range(0, len(content), chunk_size)
            ]

            index_result = tool_index_documents(corpus_id=corpus_id, documents=chunks)

            return {
                "status": "success",
                "corpus_id": corpus_id,
                "corpus_name": name,
                "filename": filename,
                "chunks_indexed": index_result.get("indexed_count", 0),
                "total_documents": index_result.get("total_documents", 0),
                "message": f"File '{filename}' ingested into corpus '{name}'. Use tool_search_corpus with corpus_id='{corpus_id}' to answer questions.",
            }

        else:
            logger.info("==================================================USING S3 NOW in _make_ingest_file===================================================")
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
            ext = os.path.splitext(filename)[1].lower()
            content = None

            if ext == ".pdf":
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                    tmp.write(content_bytes)
                    tmp_path = tmp.name

                try:
                    content = _extract_pdf_text(tmp_path)
                finally:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
            else:
                for encoding in ["utf-8", "cp1252"]:
                    try:
                        content = content_bytes.decode(encoding)
                        break
                    except UnicodeDecodeError:
                        continue

        if not content:
            return {
                "status": "error",
                "message": f"Could not extract text from '{filename}'",
            }

        # Check for existing corpus with same name
        name = corpus_name or filename
        existing = tool_list_corpora()
        corpus_id = None
        for c in existing.get("corpora", []):
            if c["name"] == name:
                corpus_id = c["corpus_id"]
                break

        # Create corpus if needed
        if not corpus_id:
            result = tool_create_corpus(
                name=name, description=f"Indexed from uploaded file: {filename}"
            )
            corpus_id = result["corpus_id"]

        # Split content into chunks for indexing
        chunk_size = 1000
        chunks = [
            content[i : i + chunk_size] for i in range(0, len(content), chunk_size)
        ]

        index_result = tool_index_documents(corpus_id=corpus_id, documents=chunks)

        return {
            "status": "success",
            "corpus_id": corpus_id,
            "corpus_name": name,
            "filename": filename,
            "chunks_indexed": index_result.get("indexed_count", 0),
            "total_documents": index_result.get("total_documents", 0),
            "message": f"File '{filename}' ingested into corpus '{name}'. Use tool_search_corpus with corpus_id='{corpus_id}' to answer questions.",
        }

    return ingest_file

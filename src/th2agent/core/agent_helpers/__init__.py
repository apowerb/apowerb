"""agent_helpers package — façade re-exporting the previously monolithic module surface.

The original ``agent_helpers.py`` was split into focused sub-modules:
- ``text_utils``: template escaping, HTML helpers, content truncation
- ``pdf_writer``: PDF generation and extraction
- ``file_readers``: image/archive/office/binary readers
- ``tool_factories``: agent-bound tool factories (create_downloadable_file, upload, text-to-SQL)
- ``read_file_tool``: ``_make_read_uploaded_file`` factory
- ``ingest_file_tool``: ``_make_ingest_file`` factory
- ``tools_binder``: rebinding helpers used by ``to_agent``
- ``mcp_loader``: MCP server toolset loader
- ``llm_model_builder``: LiteLlm construction and output-schema injection
- ``extras_loader``: skills, SuperAgent recommended tools, BI dashboard tools
- ``agent_utils``: agent details, model params, notifications, ``to_agent`` builder

All symbols previously importable as ``th2agent.core.agent_helpers.X`` remain
available for backwards compatibility.
"""
from __future__ import annotations

from th2agent.core.agent_helpers.text_utils import (
    _MAX_CONTENT_CHARS,
    _escape_template_vars,
    _escape_html,
    _inline_format,
    _md_to_html,
    _truncate_content,
)
from th2agent.core.agent_helpers.pdf_writer import (
    _setup_pdf_font,
    _write_pdf,
    _extract_pdf_text,
)
from th2agent.core.agent_helpers.file_readers import (
    _IMAGE_EXTENSIONS,
    _AUDIO_EXTENSIONS,
    _ARCHIVE_EXTENSIONS,
    _MAX_IMAGE_BASE64_SIZE,
    _is_binary_by_magic,
    _read_image,
    _read_archive,
    _read_excel,
    _read_docx,
    _read_pptx,
    _read_binary_metadata,
)
from th2agent.core.agent_helpers.tool_factories import (
    _TEXT_TO_SQL_TOOL_NAMES,
    _make_create_downloadable_file,
    _make_upload_file,
    _make_ingest_file,
    _make_read_uploaded_file,
    _resolve_text_to_sql_tools,
    logger,
)
from th2agent.core.agent_helpers.agent_utils import (
    _GOOGLE_TOOL_PROVIDER_MAP,
    VALID_INTEGRATION_PROVIDERS,
    agent_store,
    get_agent_details,
    load_agent_model_params,
    set_model_params_as_envvar,
    notify_user,
    request_integration,
    _inject_google_integration_tokens,
    to_agent,
)


__all__ = [
    # text utils
    "_MAX_CONTENT_CHARS",
    "_escape_template_vars",
    "_escape_html",
    "_inline_format",
    "_md_to_html",
    "_truncate_content",
    # pdf
    "_setup_pdf_font",
    "_write_pdf",
    "_extract_pdf_text",
    # file readers
    "_IMAGE_EXTENSIONS",
    "_AUDIO_EXTENSIONS",
    "_ARCHIVE_EXTENSIONS",
    "_MAX_IMAGE_BASE64_SIZE",
    "_is_binary_by_magic",
    "_read_image",
    "_read_archive",
    "_read_excel",
    "_read_docx",
    "_read_pptx",
    "_read_binary_metadata",
    # tool factories
    "_TEXT_TO_SQL_TOOL_NAMES",
    "_make_create_downloadable_file",
    "_make_upload_file",
    "_make_ingest_file",
    "_make_read_uploaded_file",
    "_resolve_text_to_sql_tools",
    # agent utils
    "_GOOGLE_TOOL_PROVIDER_MAP",
    "agent_store",
    "get_agent_details",
    "load_agent_model_params",
    "set_model_params_as_envvar",
    "notify_user",
    "VALID_INTEGRATION_PROVIDERS",
    "request_integration",
    "_inject_google_integration_tokens",
    "to_agent",
    "logger",
]

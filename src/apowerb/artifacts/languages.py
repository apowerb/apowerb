"""One mapping from filename to language, for every producer and reader.

The tab uses it to pick a syntax highlighter, decide whether to offer an
HTML preview, and whether a run button applies. It lived in two places with
different contents: the artifacts router knew five extensions and not
`.html`, so a generated report -- the most common thing agents produce --
was listed as plain text and lost its preview.
"""

from __future__ import annotations

import os

_BY_EXTENSION = {
    ".py": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".ts": "typescript",
    ".sh": "bash",
    ".bash": "bash",
    ".rb": "ruby",
    ".go": "go",
    ".sql": "sql",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".md": "markdown",
    ".csv": "csv",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".xml": "xml",
    ".pdf": "pdf",
}


def language_for_filename(filename: str) -> str:
    _, ext = os.path.splitext(filename)
    return _BY_EXTENSION.get(ext.lower(), "text")

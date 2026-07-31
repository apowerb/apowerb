"""Text and template helpers shared across agent helpers."""
from __future__ import annotations

import re


_MAX_CONTENT_CHARS = 50_000  # ~12-15k tokens, safe margin under context limits


def _escape_template_vars(instruction: str) -> str:
    """Escape ${var} patterns in instructions to prevent ADK from
    interpreting them as session state variables.

    ADK uses {var_name} syntax for session state injection.
    Template variables like ${sender} from webhook templates
    should not be in instructions, but if they are, we escape
    them to prevent KeyError crashes.
    """
    if not instruction:
        return instruction
    # Replace ${identifier} with [identifier] to prevent ADK state resolution
    return re.sub(r'\$\{(\w+)\}', r'[\1]', instruction)


def _escape_html(text: str) -> str:
    """Escape HTML special characters."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _inline_format(text: str) -> str:
    """Convert inline markdown (**bold**) to HTML, escaping the rest."""
    parts = re.split(r"(\*\*.*?\*\*)", text)
    result = []
    for part in parts:
        if part.startswith("**") and part.endswith("**"):
            result.append(f"<b>{_escape_html(part[2:-2])}</b>")
        else:
            result.append(_escape_html(part))
    return "".join(result)


def _md_to_html(content: str) -> str:
    """Convert simple markdown to HTML suitable for fpdf2.write_html()."""
    html_lines = []
    for line in content.split("\n"):
        stripped = line.strip()
        if stripped.startswith("### "):
            html_lines.append(f"<h3>{_escape_html(stripped[4:])}</h3>")
        elif stripped.startswith("## "):
            html_lines.append(f"<h2>{_escape_html(stripped[3:])}</h2>")
        elif stripped.startswith("# "):
            html_lines.append(f"<h1>{_escape_html(stripped[2:])}</h1>")
        elif stripped.startswith("- ") or stripped.startswith("* "):
            html_lines.append(f"<br>&nbsp;&nbsp;-&nbsp;{_inline_format(stripped[2:])}")
        elif stripped == "":
            html_lines.append("<br><br>")
        else:
            html_lines.append(f"<p>{_inline_format(stripped)}</p>")
    return "".join(html_lines)


def _truncate_content(
    content: str, max_chars: int = _MAX_CONTENT_CHARS
) -> tuple[str, bool]:
    """Truncate content to max_chars, returns (content, was_truncated)."""
    if len(content) <= max_chars:
        return content, False
    return content[
        :max_chars
    ] + "\n\n[... CONTENT TRUNCATED — showing first 50,000 characters ...]", True

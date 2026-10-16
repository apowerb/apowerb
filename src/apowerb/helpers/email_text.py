"""Plain-text rendering of an email body for tool responses."""

import html
import re

MAX_BODY_CHARS = 15_000

_STYLE_OR_SCRIPT = re.compile(r"<(style|script)\b.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"<[^>]+>")


def html_to_text(raw: str) -> str:
    """Drop <style>/<script> blocks and tags, then decode entities."""
    return html.unescape(_TAG.sub("", _STYLE_OR_SCRIPT.sub("", raw))).strip()


def cap_body(text: str) -> tuple[str, bool]:
    """Return ``text`` cut to ``MAX_BODY_CHARS`` and whether it was cut."""
    return text[:MAX_BODY_CHARS], len(text) > MAX_BODY_CHARS

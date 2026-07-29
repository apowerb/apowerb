"""Single, strict filename sanitizer used everywhere a user/attacker-
controlled name hits the filesystem.

Was duplicated as ``_safe_filename`` in ``storage/webhook_attachments.py``
and as ``_sanitize_filename`` in ``tools_store/portfolio/outlook_mail.py``.
The two implementations diverged enough that the cache-lookup path of PR 2
(serve the file the webhook handler stored to the agent's uploads dir)
would have collided silently on names with special characters. This module
is the single source of truth.

Contract — what the result is guaranteed to satisfy:

  * No path components — ``os.path.basename`` upstream.
  * No NUL bytes, no other non-printable characters.
  * No path-confusable characters: ``/``, ``\\``, ``..``.
  * Only word chars, spaces, dashes and dots remain — everything else
    is replaced with ``_`` (covers ``#``, ``%``, control chars after
    URL decoding by FastAPI, etc.).
  * No leading dot (so the result is never a dotfile).
  * No trailing dot or space (avoids Windows reserved patterns).
  * Never empty: falls back to ``"attachment"``.
"""

from __future__ import annotations

import os
import re


_DISALLOWED_RE = re.compile(r"[^\w\s\-.]", flags=re.UNICODE)


def sanitize_filename(raw: str | None) -> str:
    """Return a leaf filename safe to write to disk and route through HTTP."""
    if not raw:
        return "attachment"
    # Strip directory components on any platform.
    name = os.path.basename(raw)
    # Drop NUL bytes and control characters before the regex below — the
    # regex's ``\w``/``\s`` classes wouldn't reject them on their own.
    name = "".join(ch for ch in name if ch.isprintable())
    # Defence in depth against parent traversal — basename already
    # protects us but a literal ``..`` in the middle of a name would
    # still resolve oddly with some downstream consumers.
    name = name.replace("..", "_")
    # Replace any character that isn't a word char, whitespace, dash or
    # dot with ``_``. Catches '/', '\\', '#', '%', '*', '?' and everything
    # else operating systems get nervous about.
    name = _DISALLOWED_RE.sub("_", name)
    # No leading dot (dotfile), no trailing dot/space (Windows).
    name = name.lstrip(".")
    name = name.rstrip(". ")
    return name or "attachment"

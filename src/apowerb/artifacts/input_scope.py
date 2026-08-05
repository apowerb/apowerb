"""Resolves the S3 scope segment for an uploaded input artifact.

Option C (David, PR review 05/08): the upload API (routers/files.py) does
not receive a session_id today and must keep working for callers that never
send one. A known session_id gets its own scope; an unknown one falls back
to a shared, agent-wide scope -- not a fabricated session_id, and never
`None` passed further down (S3ArtifactService._key_prefix raises on a
`None` session_id for session-scoped artifacts).
"""

from __future__ import annotations

SHARED_INPUT_SCOPE = "_shared"


def resolve_input_session_id(session_id: str | None) -> str:
    return session_id if session_id else SHARED_INPUT_SCOPE

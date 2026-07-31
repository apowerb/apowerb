"""Shared validators, constants, and locks for the RAG router endpoints.

Groups the security helpers (agent ownership, session_id sanitization, SSRF
protection) and the module-level state (file size cap, environment-mutation
locks) reused across the RAG endpoint sub-modules.
"""

import asyncio
import ipaddress
import re
from logging import getLogger
from urllib.parse import urlparse

from fastapi import HTTPException

from apowerb.users import schemas as user_schemas

logger = getLogger(__name__)

# ---------------------------------------------------------------------------
# S1e — Maximum upload file size (50 MB)
# ---------------------------------------------------------------------------
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB

# ---------------------------------------------------------------------------
# S1c — Locks to prevent race conditions on os.environ for DB / S3 creds
# ---------------------------------------------------------------------------
_db_index_lock = asyncio.Lock()
_s3_index_lock = asyncio.Lock()


def _validate_session_id(session_id: str | None) -> None:
    """Sanitize session_id against path traversal — must match session_DIGITS."""
    if session_id is None:
        return
    if not re.match(r"^session_\d+$", session_id):
        raise HTTPException(status_code=400, detail="Invalid session_id format")


async def _validate_agent_ownership(agent_id: str, current_user: user_schemas.User) -> None:
    """Verify *agent_id* format, existence, and ownership by *current_user*.

    Raises:
        HTTPException 400 – invalid agent_id format (S1b path traversal guard)
        HTTPException 404 – agent does not exist
        HTTPException 403 – agent belongs to another user
    """
    # S1b: sanitize agent_id against path traversal
    if not re.match(r"^agent\d+$", agent_id):
        logger.warning("[RAG_SECURITY] Invalid agent_id format: %r from user %s", agent_id, current_user.email)
        raise HTTPException(status_code=400, detail="Invalid agent_id format")

    # Lazy import to avoid circular dependency at module level
    from apowerb.core.agent_main import agent_store

    numeric_id = int(agent_id.replace("agent", ""))

    # Query WITHOUT owner_id filter so we can distinguish 404 from 403
    select_query = agent_store.agent_table.select().where(
        agent_store.agent_table.c.agent_id == numeric_id,
    )
    rows = await asyncio.to_thread(agent_store.get_list_agents, select_query)
    if not rows:
        raise HTTPException(status_code=404, detail="Agent not found")

    agent = rows[0]._asdict()
    if str(agent.get("owner_id")) != str(current_user.email):
        logger.warning(
            "[RAG_SECURITY] Ownership denied: user %s tried to access agent %s (owner=%s)",
            current_user.email, agent_id, agent.get("owner_id"),
        )
        raise HTTPException(status_code=403, detail="Not your agent")


# ---------------------------------------------------------------------------
# S1g — SSRF protection: block requests to internal / private networks
# ---------------------------------------------------------------------------

def _validate_url_not_internal(url: str) -> None:
    """Block SSRF: reject localhost, private IPs, and non-HTTP schemes."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="Only HTTP/HTTPS URLs are allowed")
    hostname = parsed.hostname or ""
    if hostname in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
        raise HTTPException(status_code=400, detail="URLs pointing to localhost are not allowed")
    try:
        ip = ipaddress.ip_address(hostname)
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            raise HTTPException(status_code=400, detail="URLs pointing to private networks are not allowed")
    except ValueError:
        pass  # hostname is a domain, not an IP — OK

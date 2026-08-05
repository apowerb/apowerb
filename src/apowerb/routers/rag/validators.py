"""Shared validators, constants, and locks for the RAG router endpoints.

Groups the security helpers (agent ownership, session_id sanitization, SSRF
protection) and the module-level state (file size cap, environment-mutation
locks) reused across the RAG endpoint sub-modules.
"""

import asyncio
import ipaddress
import re
import socket
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
#
# Two bypasses of the original IP-literal-only check, both real (not just
# theoretical CodeQL noise):
#   1. DNS rebinding — a domain name (not a literal IP) can resolve to a
#      private/link-local/loopback address (e.g. the 169.254.169.254 cloud
#      metadata endpoint). The old check only inspected the hostname string
#      and never resolved it, so any attacker-controlled domain sailed
#      through.
#   2. Redirects — httpx.AsyncClient(follow_redirects=True) re-requests
#      whatever Location header the server returns, without re-validating
#      it. An external URL that 302s to an internal one bypassed the
#      up-front check entirely. index_url.py now revalidates every hop
#      (see _fetch_url_with_ssrf_guard) instead of blindly following.
# ---------------------------------------------------------------------------

def _is_disallowed_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _validate_url_not_internal(url: str) -> None:
    """Block SSRF: reject localhost, private/reserved IPs (including via DNS
    resolution of the hostname), and non-HTTP schemes."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="Only HTTP/HTTPS URLs are allowed")
    hostname = parsed.hostname or ""
    if not hostname:
        raise HTTPException(status_code=400, detail="URL must include a hostname")
    if hostname in ("localhost", "0.0.0.0"):
        raise HTTPException(status_code=400, detail="URLs pointing to localhost are not allowed")

    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        ip = None  # hostname is a domain name, not an IP literal

    if ip is not None:
        if _is_disallowed_ip(ip):
            raise HTTPException(status_code=400, detail="URLs pointing to private networks are not allowed")
        return

    # Domain name: resolve it and check every address it can come back as.
    # A DNS answer under attacker control (their own domain, or a rebinding
    # attack against a domain they don't fully control) must not be able to
    # point this server at its own private network.
    try:
        addr_infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        raise HTTPException(status_code=400, detail="Could not resolve URL hostname")

    for family, _type, _proto, _canonname, sockaddr in addr_infos:
        raw_ip = sockaddr[0]
        try:
            resolved_ip = ipaddress.ip_address(raw_ip)
        except ValueError:
            continue
        if _is_disallowed_ip(resolved_ip):
            raise HTTPException(
                status_code=400,
                detail="URLs resolving to private networks are not allowed",
            )

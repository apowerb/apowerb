"""RAG tools — Knowledge base management via the Thaink2 RAG API (rag.thaink2.fr).

Provides 5 tools for creating, listing, getting, deleting, and searching
knowledge bases backed by the hosted RAG service.

Auth credentials: environment variables ``th2username`` / ``th2password``.
Falls back to the current user's email (``AGENT_OWNER`` env var) for both.
"""

import os
import time
from logging import getLogger

import httpx

logger = getLogger(__name__)

_RAG_BASE_URL = os.environ.get("RAG_BASE_URL", "https://rag-dev.thaink2.fr")
_POLL_INTERVAL = 3  # seconds between readiness polls
_DEFAULT_MAX_WAIT = 120  # default poll timeout


# ---------------------------------------------------------------------------
# Internal auth helpers (self-contained — no dependency on thaink2.py)
# ---------------------------------------------------------------------------

_SERVICE_ACCOUNT_EMAIL = "th2agent-service@thaink2.com"


def _login() -> str:
    """Authenticate against the RAG API and return a bearer token.

    Uses a shared service account (th2agent-service@thaink2.com) by default.
    Can be overridden with th2username/th2password env vars.
    If the service account does not exist, auto-creates it.
    """
    username = os.environ.get("th2username", "") or _SERVICE_ACCOUNT_EMAIL
    password = os.environ.get("th2password", "") or _SERVICE_ACCOUNT_EMAIL

    resp = httpx.post(
        f"{_RAG_BASE_URL}/auth/token",
        data={"username": username, "password": password},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )

    if resp.status_code == 200:
        token = resp.json().get("access_token")
        if token:
            return token

    # Auto-create the service account if it doesn't exist
    body = resp.text.lower()
    if resp.status_code == 404 or "does not exist" in body:
        _ensure_service_account()
        resp = httpx.post(
            f"{_RAG_BASE_URL}/auth/token",
            data={"username": username, "password": password},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )

    resp.raise_for_status()
    token = resp.json().get("access_token")
    if not token:
        raise RuntimeError("RAG API login succeeded but no access_token returned.")
    return token


def _ensure_service_account() -> None:
    """Create the shared th2agent service account on the RAG API if missing."""
    try:
        httpx.post(
            f"{_RAG_BASE_URL}/users/",
            json={
                "first_name": "th2agent",
                "last_name": "service",
                "email": _SERVICE_ACCOUNT_EMAIL,
                "password": _SERVICE_ACCOUNT_EMAIL,
            },
            timeout=15,
        )
    except Exception:
        pass  # best-effort — login retry will surface the real error


def _auth_headers() -> dict[str, str]:
    """Return Authorization header dict with a fresh bearer token."""
    token = _login()
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Public tools (auto-discovered by the tool loader via the ``tool_`` prefix)
# ---------------------------------------------------------------------------

def tool_create_knowledge(
    name: str,
    description: str,
    files: list[str],
    prompt: str = "You are a helpful assistant",
    wait_for_completion: bool = True,
    max_wait_seconds: int = _DEFAULT_MAX_WAIT,
    callback_url: str | None = None,
) -> dict:
    """Create a knowledge base and index the provided files via the Thaink2 RAG API.

    Uploads the given files, triggers server-side indexing, and optionally polls
    until processing is complete.

    Args:
        name: Human-readable name for the knowledge base.
        description: Description of the content being indexed.
        files: List of local file paths to upload and index.
        prompt: System prompt used by the RAG engine when answering queries.
        wait_for_completion: If True, poll until indexing finishes or timeout.
        max_wait_seconds: Maximum seconds to wait when polling (default 120).
        callback_url: Optional webhook URL for th2llm to notify on completion.

    Returns:
        dict with keys: status, knowledge_id, elapsed_seconds, message.
    """
    try:
        headers = _auth_headers()
    except Exception as e:
        return {"status": "error", "message": f"RAG authentication failed: {e}. Do not retry — this is a configuration issue.", "retry": False}

    # Build multipart payload with explicit MIME types
    import mimetypes
    MIME_OVERRIDES = {".csv": "text/csv", ".tsv": "text/tab-separated-values", ".md": "text/markdown"}
    multipart_files = []
    for fpath in files:
        if not os.path.exists(fpath):
            return {"status": "error", "message": f"File not found: {fpath}. Verify the path is correct. Do not retry with the same path.", "retry": False}
        fname = os.path.basename(fpath)
        ext = os.path.splitext(fname)[1].lower()
        mime_type = MIME_OVERRIDES.get(ext) or mimetypes.guess_type(fname)[0] or "application/octet-stream"
        multipart_files.append(("files", (fname, open(fpath, "rb"), mime_type)))

    form_data: dict[str, str] = {"name": name, "description": description, "prompt": prompt}
    if callback_url:
        form_data["callback_url"] = callback_url
        logger.info("[RAG] Including callback_url=%s in knowledge creation request", callback_url)

    try:
        resp = httpx.post(
            f"{_RAG_BASE_URL}/knowledge/",
            headers=headers,
            data=form_data,
            files=multipart_files,
            timeout=60,
        )

        # --- Detailed error logging before raise_for_status ---
        if resp.status_code >= 400:
            response_body = resp.text
            logger.error(
                "[RAG] POST /knowledge/ failed — HTTP %d\n"
                "  Request fields: name=%r, description=%r, prompt=%r, files=%r\n"
                "  Response body: %s",
                resp.status_code,
                name,
                description[:200],
                prompt[:100],
                [f for f in files],
                response_body,
            )
            return {
                "status": "error",
                "message": (
                    f"RAG API returned HTTP {resp.status_code} on POST /knowledge/. "
                    f"Response: {response_body}. Do not retry — inform the user."
                ),
                "retry": False,
            }

        data = resp.json()
        # API nests the knowledge under "result" key
        result = data.get("result", data)
        knowledge_id = result.get("knowledge_id") or result.get("id") or data.get("knowledge_id")
        logger.info(
            "[RAG] Knowledge created successfully: id=%s, name=%r",
            knowledge_id, name,
        )
    except httpx.HTTPStatusError as e:
        response_body = e.response.text if e.response is not None else "N/A"
        logger.error(
            "[RAG] HTTPStatusError on POST /knowledge/ — %s\n  Response body: %s",
            e, response_body,
        )
        return {
            "status": "error",
            "message": f"Failed to create knowledge (HTTP {e.response.status_code}): {response_body}. Do not retry — inform the user.",
            "retry": False,
        }
    except Exception as e:
        logger.error("[RAG] Unexpected error on POST /knowledge/ — %s", e, exc_info=True)
        return {"status": "error", "message": f"Failed to create knowledge: {e}. Do not retry — inform the user.", "retry": False}
    finally:
        for _, (_, fobj, _) in multipart_files:
            fobj.close()

    if not wait_for_completion:
        return {
            "status": "processing",
            "knowledge_id": knowledge_id,
            "elapsed_seconds": 0,
            "message": "Knowledge base created. Indexing started (not waiting).",
        }

    # Poll for completion
    start = time.time()
    while time.time() - start < max_wait_seconds:
        time.sleep(_POLL_INTERVAL)
        try:
            poll = httpx.get(
                f"{_RAG_BASE_URL}/knowledge/{knowledge_id}",
                headers=_auth_headers(),
                timeout=30,
            )
            poll.raise_for_status()
            data = poll.json()
            if data.get("is_complete") or str(data.get("status", "")).upper() == "COMPLETED":
                elapsed = round(time.time() - start, 1)
                return {
                    "status": "complete",
                    "knowledge_id": knowledge_id,
                    "elapsed_seconds": elapsed,
                    "message": f"Knowledge base '{name}' indexed successfully in {elapsed}s.",
                }
        except Exception:
            pass  # transient errors — keep polling

    elapsed = round(time.time() - start, 1)
    return {
        "status": "timeout",
        "knowledge_id": knowledge_id,
        "elapsed_seconds": elapsed,
        "message": f"Indexing still in progress after {elapsed}s. Use tool_get_knowledge to check status.",
    }


def tool_list_knowledge() -> dict:
    """List all available knowledge bases.

    Returns:
        dict with keys: status, knowledge_bases (list), total (int).
    """
    try:
        headers = _auth_headers()
        resp = httpx.get(
            f"{_RAG_BASE_URL}/knowledge/",
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        # API returns {"results": [...], "total": N} or a list
        if isinstance(data, list):
            items = data
        else:
            items = data.get("results", data.get("items", data.get("knowledge_bases", [])))
        return {"status": "success", "knowledge_bases": items, "total": len(items)}
    except Exception as e:
        return {"status": "error", "message": f"Failed to list knowledge bases: {e}. Do not retry — inform the user.", "retry": False}


def tool_get_knowledge(knowledge_id: str) -> dict:
    """Get details and processing status of a knowledge base.

    Args:
        knowledge_id: The ID of the knowledge base to retrieve.

    Returns:
        dict with keys: status, knowledge_id, name, is_complete, raw.
    """
    try:
        headers = _auth_headers()
        resp = httpx.get(
            f"{_RAG_BASE_URL}/knowledge/{knowledge_id}",
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "status": "success",
            "knowledge_id": knowledge_id,
            "name": data.get("name", ""),
            "is_complete": bool(data.get("is_complete") or str(data.get("status", "")).upper() == "COMPLETED"),
            "raw": data,
        }
    except Exception as e:
        return {"status": "error", "message": f"Failed to get knowledge base: {e}. Do not retry — inform the user.", "retry": False}


def tool_delete_knowledge(knowledge_id: str) -> dict:
    """Delete a knowledge base and all its indexed documents.

    Args:
        knowledge_id: The ID of the knowledge base to delete.

    Returns:
        dict with keys: status, message.
    """
    try:
        headers = _auth_headers()
        resp = httpx.delete(
            f"{_RAG_BASE_URL}/knowledge/{knowledge_id}",
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        return {"status": "success", "message": f"Knowledge base {knowledge_id} deleted."}
    except Exception as e:
        return {"status": "error", "message": f"Failed to delete knowledge base: {e}. Do not retry — inform the user.", "retry": False}


def tool_search_knowledge(
    knowledge_id: str,
    query: str,
    conversation_id: str = "",
) -> dict:
    """Search a knowledge base using RAG — creates a conversation if needed.

    Verifies the knowledge base is ready, creates a conversation linked to the
    knowledge_id, sends the query, and returns the RAG answer.

    Args:
        knowledge_id: The knowledge base to query.
        query: The user's question.
        conversation_id: Optional existing conversation ID for multi-turn context.
            Leave empty to start a new conversation.

    Returns:
        dict with keys: status, answer, conversation_id, knowledge_id.
    """
    try:
        headers = _auth_headers()
    except Exception as e:
        return {"status": "error", "message": f"RAG authentication failed: {e}. Do not retry — this is a configuration issue.", "retry": False}

    # 1. Verify readiness
    try:
        check = httpx.get(
            f"{_RAG_BASE_URL}/knowledge/{knowledge_id}",
            headers=headers,
            timeout=30,
        )
        check.raise_for_status()
        check_data = check.json()
        is_ready = check_data.get("is_complete") or str(check_data.get("status", "")).upper() == "COMPLETED"
        if not is_ready:
            return {
                "status": "error",
                "message": "Knowledge base is not ready yet. Indexing is still in progress. Inform the user to wait and try again later. Do not retry immediately.",
                "knowledge_id": knowledge_id,
                "retry": False,
            }
    except Exception as e:
        return {"status": "error", "message": f"Failed to check knowledge readiness: {e}. Do not retry — inform the user.", "retry": False}

    # 2. Create conversation if needed (linked to knowledge_id for document RAG)
    if not conversation_id:
        try:
            conv_resp = httpx.post(
                f"{_RAG_BASE_URL}/conversations",
                headers=headers,
                json={"title": query[:100], "knowledge_id": int(knowledge_id)},
                timeout=30,
            )
            conv_resp.raise_for_status()
            conv_data = conv_resp.json()
            conversation_id = str(conv_data.get("id") or conv_data.get("conversation_id"))
        except Exception as e:
            return {"status": "error", "message": f"Failed to create conversation: {e}. Do not retry — inform the user.", "retry": False}

    # 3. Send message and get RAG answer
    try:
        msg_resp = httpx.post(
            f"{_RAG_BASE_URL}/conversations/{conversation_id}/messages",
            headers=headers,
            json={"content": query, "sender": "USER"},
            timeout=120,
        )
        msg_resp.raise_for_status()
        msg_data = msg_resp.json()

        # The API returns the RAG answer in the response
        answer = msg_data.get("content") or msg_data.get("answer") or msg_data.get("message", "")
        sender = msg_data.get("sender", "")

        return {
            "status": "success",
            "answer": answer,
            "sender": sender,
            "conversation_id": conversation_id,
            "knowledge_id": knowledge_id,
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed to query knowledge base: {e}. Do not retry — inform the user.",
            "conversation_id": conversation_id,
            "knowledge_id": knowledge_id,
            "retry": False,
        }

"""Google Gmail tools -- Read, search, and send emails via Gmail API.

Provides 4 tools for listing, reading, searching, and sending emails
from a connected Google / Gmail account.

Auth credentials are injected as environment variables by the tool_config
system at agent runtime:
  - ``GOOGLE_GMAIL_REFRESH_TOKEN`` -- OAuth2 refresh token

Client ID and client secret are read from application settings at runtime
(not stored per-tool).

The shared helper ``google_auth`` transparently exchanges the refresh token
for a short-lived access token and caches it for ~50 minutes.
"""

import base64
import re
from email.mime.text import MIMEText
from logging import getLogger

import httpx

from th2agent.tools_store.portfolio.google_auth import google_auth_headers
from th2agent.tools_store.portfolio.integration_status import IntegrationStatusError

logger = getLogger(__name__)

_GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
_SERVICE_PREFIX = "GOOGLE_GMAIL"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_header(headers: list[dict], name: str) -> str:
    """Extract a specific header value from a Gmail message headers list."""
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _format_message_summary(msg: dict) -> dict:
    """Format a Gmail message (metadata format) into a summary dict."""
    headers = msg.get("payload", {}).get("headers", [])
    return {
        "id": msg.get("id"),
        "subject": _extract_header(headers, "Subject"),
        "from": _extract_header(headers, "From"),
        "date": _extract_header(headers, "Date"),
        "snippet": msg.get("snippet", ""),
        "labelIds": msg.get("labelIds", []),
    }


def _decode_body_part(part: dict) -> str:
    """Decode a base64url-encoded body part from a Gmail message."""
    data = part.get("body", {}).get("data", "")
    if not data:
        return ""
    # Gmail uses URL-safe base64 without padding
    padded = data + "=" * (4 - len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _extract_body(payload: dict) -> str:
    """Extract the best text body from a Gmail message payload.

    Prefers text/plain, falls back to text/html with tags stripped.
    Recursively walks multipart payloads.
    """
    mime_type = payload.get("mimeType", "")

    # Simple single-part message
    if mime_type == "text/plain":
        return _decode_body_part(payload)
    if mime_type == "text/html":
        html = _decode_body_part(payload)
        return re.sub(r"<[^>]+>", "", html).strip()

    # Multipart: walk parts looking for text/plain first, then text/html
    parts = payload.get("parts", [])
    plain_text = ""
    html_text = ""
    for part in parts:
        part_mime = part.get("mimeType", "")
        if part_mime == "text/plain" and not plain_text:
            plain_text = _decode_body_part(part)
        elif part_mime == "text/html" and not html_text:
            html_text = _decode_body_part(part)
        elif part_mime.startswith("multipart/"):
            # Recurse into nested multipart
            nested = _extract_body(part)
            if nested and not plain_text:
                plain_text = nested

    if plain_text:
        return plain_text
    if html_text:
        return re.sub(r"<[^>]+>", "", html_text).strip()
    return ""


def _extract_attachments(payload: dict) -> list[dict]:
    """Extract attachment metadata from a Gmail message payload."""
    attachments = []
    parts = payload.get("parts", [])
    for part in parts:
        filename = part.get("filename", "")
        if filename:
            body = part.get("body", {})
            attachments.append({
                "id": body.get("attachmentId", ""),
                "filename": filename,
                "mimeType": part.get("mimeType", ""),
                "size": body.get("size", 0),
            })
        # Recurse into nested parts
        if part.get("parts"):
            attachments.extend(_extract_attachments(part))
    return attachments


# ---------------------------------------------------------------------------
# Public tools (auto-discovered via the ``tool_`` prefix)
# ---------------------------------------------------------------------------


def tool_list_emails(
    max_results: int = 10,
    label: str = "INBOX",
    query: str | None = None,
) -> dict:
    """List recent emails from a Gmail label/folder.

    Retrieves the most recent emails from the specified label with optional
    Gmail search query filtering.

    Args:
        max_results: Maximum number of emails to return (1-50). Defaults to 10.
        label: Gmail label to read from (e.g. "INBOX", "SENT", "DRAFTS",
            "STARRED", "IMPORTANT"). Defaults to "INBOX".
        query: Optional Gmail search query (same syntax as Gmail search bar,
            e.g. "from:john@example.com", "subject:invoice", "is:unread",
            "after:2026/01/01"). If omitted, returns most recent emails.

    Returns:
        dict with keys: status, emails (list of summaries), total.
    """
    try:
        headers = google_auth_headers(_SERVICE_PREFIX)
    except IntegrationStatusError as e:
        return e.as_tool_result()
    except RuntimeError as e:
        return {"status": "error", "message": str(e), "retry": False}

    max_results = max(1, min(max_results, 50))
    params: dict[str, str] = {
        "labelIds": label,
        "maxResults": str(max_results),
    }
    if query:
        params["q"] = query

    try:
        # Step 1: Get message IDs
        resp = httpx.get(
            f"{_GMAIL_BASE}/messages",
            headers=headers,
            params=params,
            timeout=30,
        )
        if resp.status_code == 401:
            return {
                "status": "error",
                "message": "Authentication expired. The user needs to reconnect their Google account. Do not retry.",
                "retry": False,
            }
        if resp.status_code >= 400:
            return {
                "status": "error",
                "message": f"Gmail API error (HTTP {resp.status_code}): {resp.text[:300]}. Do not retry — inform the user.",
                "retry": False,
            }

        data = resp.json()
        message_ids = data.get("messages", [])
        if not message_ids:
            return {"status": "success", "emails": [], "total": 0}

        # Step 2: Fetch each message with metadata format
        emails = []
        for msg_ref in message_ids:
            msg_resp = httpx.get(
                f"{_GMAIL_BASE}/messages/{msg_ref['id']}",
                headers=headers,
                params={"format": "metadata", "metadataHeaders": "Subject,From,Date"},
                timeout=30,
            )
            if msg_resp.status_code == 200:
                emails.append(_format_message_summary(msg_resp.json()))

        return {"status": "success", "emails": emails, "total": len(emails)}
    except httpx.TimeoutException:
        return {
            "status": "error",
            "message": "Request timed out. The Gmail service may be slow. You may retry once.",
            "retry": True,
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed to list emails: {e}. Do not retry — inform the user.",
            "retry": False,
        }


def tool_read_email(message_id: str) -> dict:
    """Read the full content of a specific Gmail message by its ID.

    Use this after tool_list_emails or tool_search_emails to get the complete
    body, recipients, and attachment metadata of a particular message.

    Args:
        message_id: The unique ID of the email message (from tool_list_emails
            or tool_search_emails results).

    Returns:
        dict with keys: status, id, subject, from, to, date, body,
        attachments (list of {id, filename, mimeType, size}).
    """
    if not message_id or not message_id.strip():
        return {
            "status": "error",
            "message": "message_id is required. Do not retry with an empty value.",
            "retry": False,
        }

    try:
        headers = google_auth_headers(_SERVICE_PREFIX)
    except IntegrationStatusError as e:
        return e.as_tool_result()
    except RuntimeError as e:
        return {"status": "error", "message": str(e), "retry": False}

    try:
        resp = httpx.get(
            f"{_GMAIL_BASE}/messages/{message_id}",
            headers=headers,
            params={"format": "full"},
            timeout=30,
        )
        if resp.status_code == 401:
            return {
                "status": "error",
                "message": "Authentication expired. The user needs to reconnect their Google account. Do not retry.",
                "retry": False,
            }
        if resp.status_code == 404:
            return {
                "status": "error",
                "message": f"Email not found (message_id={message_id}). It may have been deleted. Do not retry with the same ID.",
                "retry": False,
            }
        if resp.status_code >= 400:
            return {
                "status": "error",
                "message": f"Gmail API error (HTTP {resp.status_code}): {resp.text[:300]}. Do not retry — inform the user.",
                "retry": False,
            }

        msg = resp.json()
        payload = msg.get("payload", {})
        msg_headers = payload.get("headers", [])

        body = _extract_body(payload)
        attachments = _extract_attachments(payload)

        return {
            "status": "success",
            "id": msg.get("id"),
            "subject": _extract_header(msg_headers, "Subject"),
            "from": _extract_header(msg_headers, "From"),
            "to": _extract_header(msg_headers, "To"),
            "date": _extract_header(msg_headers, "Date"),
            "body": body,
            "attachments": attachments,
        }
    except httpx.TimeoutException:
        return {
            "status": "error",
            "message": "Request timed out. You may retry once.",
            "retry": True,
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed to read email: {e}. Do not retry — inform the user.",
            "retry": False,
        }


def tool_send_email(to: str, subject: str, body: str) -> dict:
    """Send an email from the user's Gmail account.

    Composes and sends a plain-text email to the specified recipient.

    Args:
        to: Recipient email address (e.g. "john@example.com").
        subject: Email subject line.
        body: Plain text body of the email.

    Returns:
        dict with keys: status, message_id.
    """
    if not to or not to.strip():
        return {
            "status": "error",
            "message": "Recipient address (to) is required. Do not retry with an empty value.",
            "retry": False,
        }
    if not subject:
        return {
            "status": "error",
            "message": "Subject is required. Do not retry with an empty value.",
            "retry": False,
        }

    try:
        headers = google_auth_headers(_SERVICE_PREFIX)
    except IntegrationStatusError as e:
        return e.as_tool_result()
    except RuntimeError as e:
        return {"status": "error", "message": str(e), "retry": False}

    try:
        # Build RFC 2822 message
        mime_msg = MIMEText(body, "plain", "utf-8")
        mime_msg["To"] = to
        mime_msg["Subject"] = subject

        # Gmail API expects base64url-encoded RFC 2822
        raw = base64.urlsafe_b64encode(mime_msg.as_bytes()).decode("ascii")

        resp = httpx.post(
            f"{_GMAIL_BASE}/messages/send",
            headers={**headers, "Content-Type": "application/json"},
            json={"raw": raw},
            timeout=30,
        )
        if resp.status_code == 401:
            return {
                "status": "error",
                "message": "Authentication expired. The user needs to reconnect their Google account. Do not retry.",
                "retry": False,
            }
        if resp.status_code >= 400:
            return {
                "status": "error",
                "message": f"Gmail API error (HTTP {resp.status_code}): {resp.text[:300]}. Do not retry — inform the user.",
                "retry": False,
            }

        sent = resp.json()
        return {
            "status": "ok",
            "message_id": sent.get("id"),
        }
    except httpx.TimeoutException:
        return {
            "status": "error",
            "message": "Request timed out while sending. The email may or may not have been sent. Do not retry — inform the user to check their Sent folder.",
            "retry": False,
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed to send email: {e}. Do not retry — inform the user.",
            "retry": False,
        }


def tool_search_emails(query: str, max_results: int = 10) -> dict:
    """Search emails across the entire Gmail mailbox using Gmail search syntax.

    Uses the same powerful search syntax available in the Gmail search bar.
    Searches across subject, body, sender, labels, and more.

    Args:
        query: Gmail search query string. Examples:
            - "from:john@example.com" -- emails from a specific sender
            - "subject:invoice" -- emails with "invoice" in the subject
            - "is:unread" -- unread emails
            - "has:attachment filename:pdf" -- emails with PDF attachments
            - "after:2026/01/01 before:2026/02/01" -- date range
            - "meeting OR agenda" -- emails containing either word
        max_results: Maximum number of results to return (1-50). Defaults to 10.

    Returns:
        dict with keys: status, emails (list of summaries), total.
    """
    if not query or not query.strip():
        return {
            "status": "error",
            "message": "Search query is required. Do not retry with an empty value.",
            "retry": False,
        }

    try:
        headers = google_auth_headers(_SERVICE_PREFIX)
    except IntegrationStatusError as e:
        return e.as_tool_result()
    except RuntimeError as e:
        return {"status": "error", "message": str(e), "retry": False}

    max_results = max(1, min(max_results, 50))
    params: dict[str, str] = {
        "q": query,
        "maxResults": str(max_results),
    }

    try:
        # Step 1: Search for message IDs
        resp = httpx.get(
            f"{_GMAIL_BASE}/messages",
            headers=headers,
            params=params,
            timeout=30,
        )
        if resp.status_code == 401:
            return {
                "status": "error",
                "message": "Authentication expired. The user needs to reconnect their Google account. Do not retry.",
                "retry": False,
            }
        if resp.status_code >= 400:
            return {
                "status": "error",
                "message": f"Gmail API error (HTTP {resp.status_code}): {resp.text[:300]}. Do not retry — inform the user.",
                "retry": False,
            }

        data = resp.json()
        message_ids = data.get("messages", [])
        if not message_ids:
            return {"status": "success", "emails": [], "total": 0}

        # Step 2: Fetch each message with metadata format
        emails = []
        for msg_ref in message_ids:
            msg_resp = httpx.get(
                f"{_GMAIL_BASE}/messages/{msg_ref['id']}",
                headers=headers,
                params={"format": "metadata", "metadataHeaders": "Subject,From,Date"},
                timeout=30,
            )
            if msg_resp.status_code == 200:
                emails.append(_format_message_summary(msg_resp.json()))

        return {"status": "success", "emails": emails, "total": len(emails)}
    except httpx.TimeoutException:
        return {
            "status": "error",
            "message": "Request timed out. You may retry once.",
            "retry": True,
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed to search emails: {e}. Do not retry — inform the user.",
            "retry": False,
        }

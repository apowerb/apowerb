"""Outlook Mail tools -- Read emails via Microsoft Graph API.

Provides tools for listing, reading, searching emails, listing folders,
downloading attachments, and sending emails from a connected Outlook /
Microsoft 365 mailbox.

Auth is resolved per-invocation against the **invoker** (the user running the
agent): the Outlook refresh token is fetched from that user's DB integration
row inside ``microsoft_auth`` and used as a local value — it is never stashed
in a process-global env var, which would race across concurrent invocations and
send from the wrong mailbox (incident 2026-07-03).

Client ID and client secret are read from application settings at runtime
(not stored per-tool).

Token management (refresh, cache, error handling) is delegated to the shared
``microsoft_auth`` module.
"""

import base64
import datetime
import os
import re
from logging import getLogger

import httpx

from th2agent.configs.paths import uploads_dir
from th2agent.tools_store.portfolio.microsoft_auth import microsoft_auth_headers
from th2agent.tools_store.portfolio.integration_status import IntegrationStatusError
from th2agent.storage.filename import sanitize_filename

logger = getLogger(__name__)

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"

_OUTLOOK_SCOPE = "offline_access Mail.Read Mail.Send"
_OUTLOOK_LABEL = "Outlook Mail"

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _escape_odata(value: str) -> str:
    """Escape single quotes for OData filter expressions."""
    return value.replace("'", "''")


def _sanitize_filename(name: str) -> str:
    """Backwards-compatible alias for :func:`sanitize_filename`.

    The legacy body used a slightly looser ruleset than the common
    helper (didn't strip NUL bytes / control chars). Routing through
    the common helper aligns the names of the attachments persisted
    by the webhook handler (PR #188) and the names looked up by this
    tool when reading back from ``uploads/agent{id}/``.
    """
    return sanitize_filename(name)


def _format_email_list(messages: list[dict]) -> list[dict]:
    """Format a list of Graph API message objects into summary dicts.

    Used by both ``tool_list_emails`` and ``tool_search_emails`` to avoid
    duplicating the same mapping logic.
    """
    return [
        {
            "id": msg.get("id"),
            "subject": msg.get("subject"),
            "from": msg.get("from", {}).get("emailAddress", {}).get("address"),
            "date": msg.get("receivedDateTime"),
            "preview": msg.get("bodyPreview", "")[:200],
            "isRead": msg.get("isRead"),
            "hasAttachments": msg.get("hasAttachments"),
        }
        for msg in messages
    ]


# Short-lived per-invoker cache for the active shared mailbox, so multiple
# Graph calls in one tool invocation don't each hit the DB. Keyed by invoker
# (never a single global slot) → concurrency-safe and no cross-tenant leak.
# Cleared on integration connect/disconnect via ``reset_shared_mailbox_cache``.
_shared_mailbox_cache: dict[str, tuple[str, float]] = {}
_SHARED_MAILBOX_TTL_S = 300.0


def reset_shared_mailbox_cache() -> None:
    """Drop the shared-mailbox cache (called on integration connect/disconnect)."""
    _shared_mailbox_cache.clear()


def _active_shared_mailbox() -> str:
    """Return the CURRENT invoker's selected active shared mailbox, if any.

    Resolved per-invocation from the invoker's integration meta — never
    from a process-global env var, which would race across concurrent
    invocations and target another user's mailbox (incident 2026-07-03).
    Cached briefly per invoker to avoid a DB round-trip on every Graph call.
    Returns empty string when unset or unresolvable.
    """
    from th2agent.core.invocation_context import resolve_integration_user
    import time as _time

    invoker = resolve_integration_user(prefer_invoker=True) or ""
    now = _time.time()
    hit = _shared_mailbox_cache.get(invoker)
    if hit is not None and now < hit[1]:
        return hit[0]

    try:
        from th2agent.integrations.helpers import fetch_integration_configs
        configs = fetch_integration_configs("microsoft_outlook")
        meta = configs.get("meta") or {}
        value = meta.get("active_shared_mailbox") or ""
    except Exception:
        value = ""
    _shared_mailbox_cache[invoker] = (value, now + _SHARED_MAILBOX_TTL_S)
    return value


def _mailbox_base(mailbox: str = "") -> str:
    """Return the Graph API base path for the target mailbox.

    Resolution order: explicit mailbox argument > active shared mailbox
    configured by the user in Integrations > user's own /me mailbox.
    """
    target = mailbox or _active_shared_mailbox()
    if target:
        return f"{_GRAPH_BASE}/users/{target}"
    return f"{_GRAPH_BASE}/me"


def _graph_headers() -> dict[str, str]:
    """Return Authorization header dict for Microsoft Graph calls.

    The token is resolved per-invocation against the current invoker
    inside ``microsoft_auth`` — no process-global state to race on.
    """
    return microsoft_auth_headers(
        "OUTLOOK", scope=_OUTLOOK_SCOPE, service_label=_OUTLOOK_LABEL,
    )


# ---------------------------------------------------------------------------
# Public tools (auto-discovered via the ``tool_`` prefix)
# ---------------------------------------------------------------------------


def tool_list_emails(
    folder: str = "inbox",
    top: int = 10,
    filter_sender: str | None = None,
    filter_subject: str | None = None,
    after_date: str | None = None,
    mailbox: str = "",
) -> dict:
    """List emails from a specific Outlook mail folder.

    Retrieves the most recent emails with optional filtering. Useful to get
    an overview of new messages or to search within a specific folder.

    Args:
        folder: Mail folder to read from (e.g. "inbox", "sentitems", "drafts").
            Defaults to "inbox".
        top: Maximum number of emails to return (1-50). Defaults to 10.
        filter_sender: If provided, only return emails from this sender address.
        filter_subject: If provided, only return emails whose subject contains
            this text.
        after_date: If provided, only return emails received after this date.
            Format: "YYYY-MM-DD" (e.g. "2026-02-20").
        mailbox: If provided, read from this shared mailbox instead of the
            user's own mailbox. Pass the shared mailbox email address.

    Returns:
        dict with keys: status, emails (list of summaries), total.
    """
    try:
        headers = _graph_headers()
    except IntegrationStatusError as e:
        return e.as_tool_result()
    except RuntimeError as e:
        return {"status": "error", "message": str(e), "retry": False}

    # W4: Validate folder name to prevent URL injection
    if not re.match(r'^[a-zA-Z0-9_\-]+$', folder):
        return {"status": "error", "message": f"Invalid folder name: '{folder}'. Use simple names like 'inbox', 'sentitems', 'drafts'. Do not retry with the same value.", "retry": False}

    top = max(1, min(top, 50))
    params: dict[str, str] = {
        "$top": str(top),
        "$orderby": "receivedDateTime desc",
        "$select": "id,subject,from,receivedDateTime,bodyPreview,isRead,hasAttachments",
    }

    # Build $filter conditions (C1: escape OData values)
    filters: list[str] = []
    if filter_sender:
        filters.append(f"from/emailAddress/address eq '{_escape_odata(filter_sender)}'")
    if filter_subject:
        filters.append(f"contains(subject, '{_escape_odata(filter_subject)}')")
    if after_date:
        filters.append(f"receivedDateTime ge {after_date}T00:00:00Z")
    if filters:
        params["$filter"] = " and ".join(filters)

    url = f"{_mailbox_base(mailbox)}/mailFolders/{folder}/messages"

    try:
        resp = httpx.get(url, headers=headers, params=params, timeout=30)
        if resp.status_code == 401:
            return {
                "status": "error",
                "message": "Authentication expired. The user needs to reconnect their Outlook account. Do not retry.",
                "retry": False,
            }
        if resp.status_code >= 400:
            return {
                "status": "error",
                "message": f"Graph API error (HTTP {resp.status_code}): {resp.text[:300]}. Do not retry with the same parameters — inform the user.",
                "retry": False,
            }
        data = resp.json()
        messages = data.get("value", [])
        emails = _format_email_list(messages)
        return {"status": "success", "emails": emails, "total": len(emails)}
    except httpx.TimeoutException:
        return {"status": "error", "message": "Request timed out. The Microsoft service may be slow. You may retry once.", "retry": True}
    except Exception as e:
        return {"status": "error", "message": f"Failed to list emails: {e}. Do not retry — inform the user.", "retry": False}


def tool_read_email(message_id: str, mailbox: str = "") -> dict:
    """Read the full content of a specific email by its ID.

    Use this after tool_list_emails to get the complete body, recipients,
    and attachment metadata of a particular message.

    Args:
        message_id: The unique ID of the email message (from tool_list_emails).
        mailbox: If provided, read from this shared mailbox email address.

    Returns:
        dict with keys: status, id, subject, from, to, cc, date, body,
        isRead, hasAttachments, attachments (list of metadata).
    """
    try:
        headers = _graph_headers()
    except IntegrationStatusError as e:
        return e.as_tool_result()
    except RuntimeError as e:
        return {"status": "error", "message": str(e), "retry": False}

    params = {
        "$select": "id,subject,from,toRecipients,ccRecipients,receivedDateTime,"
        "body,isRead,hasAttachments",
    }

    try:
        resp = httpx.get(
            f"{_mailbox_base(mailbox)}/messages/{message_id}",
            headers=headers,
            params=params,
            timeout=30,
        )
        if resp.status_code == 401:
            return {
                "status": "error",
                "message": "Authentication expired. The user needs to reconnect their Outlook account. Do not retry.",
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
                "message": f"Graph API error (HTTP {resp.status_code}): {resp.text[:300]}. Do not retry — inform the user.",
                "retry": False,
            }
        msg = resp.json()

        # Extract text body (prefer text, fallback to html)
        body_obj = msg.get("body", {})
        body_text = body_obj.get("content", "")
        if body_obj.get("contentType") == "html":
            # Strip HTML for readability -- basic approach
            body_text = re.sub(r"<[^>]+>", "", body_text)
            body_text = body_text.strip()

        to_list = [
            r.get("emailAddress", {}).get("address")
            for r in msg.get("toRecipients", [])
        ]
        cc_list = [
            r.get("emailAddress", {}).get("address")
            for r in msg.get("ccRecipients", [])
        ]

        # Fetch attachment metadata if present
        attachments_meta = []
        if msg.get("hasAttachments"):
            att_resp = httpx.get(
                f"{_mailbox_base(mailbox)}/messages/{message_id}/attachments",
                headers=headers,
                params={"$select": "id,name,contentType,size"},
                timeout=30,
            )
            if att_resp.status_code == 200:
                attachments_meta = [
                    {
                        "id": a.get("id"),
                        "name": a.get("name"),
                        "contentType": a.get("contentType"),
                        "size": a.get("size"),
                    }
                    for a in att_resp.json().get("value", [])
                ]

        return {
            "status": "success",
            "id": msg.get("id"),
            "subject": msg.get("subject"),
            "from": msg.get("from", {}).get("emailAddress", {}).get("address"),
            "to": to_list,
            "cc": cc_list,
            "date": msg.get("receivedDateTime"),
            "body": body_text,
            "isRead": msg.get("isRead"),
            "hasAttachments": msg.get("hasAttachments"),
            "attachments": attachments_meta,
        }
    except httpx.TimeoutException:
        return {"status": "error", "message": "Request timed out. You may retry once.", "retry": True}
    except Exception as e:
        return {"status": "error", "message": f"Failed to read email: {e}. Do not retry — inform the user.", "retry": False}


def tool_search_emails(query: str, top: int = 10, mailbox: str = "") -> dict:
    """Search emails across the entire mailbox using a keyword query.

    Uses Microsoft Graph's $search capability which searches across subject,
    body, sender, and other fields.

    Args:
        query: The search query string (e.g. "invoice", "from:john@example.com",
            "subject:meeting").
        top: Maximum number of results to return (1-50). Defaults to 10.
        mailbox: If provided, search in this shared mailbox email address.

    Returns:
        dict with keys: status, emails (list of summaries), total.
    """
    try:
        headers = _graph_headers()
    except IntegrationStatusError as e:
        return e.as_tool_result()
    except RuntimeError as e:
        return {"status": "error", "message": str(e), "retry": False}

    top = max(1, min(top, 50))
    params: dict[str, str] = {
        "$search": f'"{query}"',
        "$top": str(top),
        "$select": "id,subject,from,receivedDateTime,bodyPreview,isRead,hasAttachments",
    }

    try:
        resp = httpx.get(
            f"{_mailbox_base(mailbox)}/messages",
            headers=headers,
            params=params,
            timeout=30,
        )
        if resp.status_code == 401:
            return {
                "status": "error",
                "message": "Authentication expired. The user needs to reconnect Outlook. Do not retry.",
                "retry": False,
            }
        if resp.status_code >= 400:
            return {
                "status": "error",
                "message": f"Graph API error (HTTP {resp.status_code}): {resp.text[:300]}. Do not retry — inform the user.",
                "retry": False,
            }
        data = resp.json()
        messages = data.get("value", [])
        emails = _format_email_list(messages)
        return {"status": "success", "emails": emails, "total": len(emails)}
    except httpx.TimeoutException:
        return {"status": "error", "message": "Request timed out. You may retry once.", "retry": True}
    except Exception as e:
        return {"status": "error", "message": f"Failed to search emails: {e}. Do not retry — inform the user.", "retry": False}


def tool_list_mail_folders(mailbox: str = "") -> dict:
    """List all mail folders in an Outlook mailbox.

    Useful to discover available folders (Inbox, Sent, Drafts, custom folders)
    and their unread counts before using tool_list_emails on a specific folder.

    Args:
        mailbox: If provided, list folders from this shared mailbox email address.

    Returns:
        dict with keys: status, folders (list with id, displayName,
        totalItemCount, unreadItemCount).
    """
    try:
        headers = _graph_headers()
    except IntegrationStatusError as e:
        return e.as_tool_result()
    except RuntimeError as e:
        return {"status": "error", "message": str(e), "retry": False}

    try:
        resp = httpx.get(
            f"{_mailbox_base(mailbox)}/mailFolders",
            headers=headers,
            params={"$select": "id,displayName,totalItemCount,unreadItemCount"},
            timeout=30,
        )
        if resp.status_code == 401:
            return {
                "status": "error",
                "message": "Authentication expired. The user needs to reconnect Outlook. Do not retry.",
                "retry": False,
            }
        if resp.status_code >= 400:
            return {
                "status": "error",
                "message": f"Graph API error (HTTP {resp.status_code}): {resp.text[:300]}. Do not retry — inform the user.",
                "retry": False,
            }
        data = resp.json()
        folders = [
            {
                "id": f.get("id"),
                "displayName": f.get("displayName"),
                "totalItems": f.get("totalItemCount"),
                "unread": f.get("unreadItemCount"),
            }
            for f in data.get("value", [])
        ]
        return {"status": "success", "folders": folders}
    except httpx.TimeoutException:
        return {"status": "error", "message": "Request timed out. You may retry once.", "retry": True}
    except Exception as e:
        return {"status": "error", "message": f"Failed to list mail folders: {e}. Do not retry — inform the user.", "retry": False}


def tool_download_attachment(
    message_id: str,
    attachment_id: str,
    save_filename: str | None = None,
    mailbox: str = "",
) -> dict:
    """Download a specific email attachment and save it to disk.

    The file is saved under the agent's uploads directory. Use tool_read_email
    first to get the attachment IDs and names.

    Args:
        message_id: The email message ID containing the attachment.
        attachment_id: The ID of the attachment to download.
        save_filename: Optional filename to save as. If omitted, uses the
            original attachment name.
        mailbox: If provided, download from this shared mailbox email address.

    Returns:
        dict with keys: status, file_path, file_name, size_bytes.
    """
    try:
        headers = _graph_headers()
    except IntegrationStatusError as e:
        return e.as_tool_result()
    except RuntimeError as e:
        return {"status": "error", "message": str(e), "retry": False}

    try:
        resp = httpx.get(
            f"{_mailbox_base(mailbox)}/messages/{message_id}/attachments/{attachment_id}",
            headers=headers,
            timeout=60,
        )
        if resp.status_code == 401:
            return {
                "status": "error",
                "message": "Authentication expired. The user needs to reconnect Outlook. Do not retry.",
                "retry": False,
            }
        if resp.status_code == 404:
            return {
                "status": "error",
                "message": f"Attachment not found (message_id={message_id}, attachment_id={attachment_id}). Do not retry with the same IDs.",
                "retry": False,
            }
        if resp.status_code >= 400:
            return {
                "status": "error",
                "message": f"Graph API error (HTTP {resp.status_code}): {resp.text[:300]}. Do not retry — inform the user.",
                "retry": False,
            }

        att_data = resp.json()

        # C4: Sanitize filename and validate resolved path
        file_name = _sanitize_filename(save_filename or att_data.get("name", "attachment"))
        content_bytes_b64 = att_data.get("contentBytes")

        if not content_bytes_b64:
            return {
                "status": "error",
                "message": "Attachment has no downloadable content (might be a reference attachment or too large). Do not retry — inform the user.",
                "retry": False,
            }

        content = base64.b64decode(content_bytes_b64)

        # W2: Use ROOT_AGENT_ID instead of AGENT_ID.
        # Folder convention: ``uploads/agent{id}/`` — must match the agent_name
        # used by factory-bound tools (pdf_to_images, read_uploaded_file,
        # create_downloadable_file). Otherwise downloaded attachments are
        # invisible to subsequent tool calls.
        agent_id = os.getenv("ROOT_AGENT_ID", "unknown_agent")
        save_dir = str(uploads_dir() / f"agent{agent_id}")
        os.makedirs(save_dir, exist_ok=True)

        file_path = os.path.join(save_dir, file_name)

        # C4: Validate that resolved path stays within save_dir
        if not os.path.abspath(file_path).startswith(os.path.abspath(save_dir)):
            return {"status": "error", "message": "Invalid filename. Do not retry.", "retry": False}

        with open(file_path, "wb") as f:
            f.write(content)

        abs_path = os.path.abspath(file_path)
        logger.info("Attachment saved: %s (%d bytes)", abs_path, len(content))

        return {
            "status": "success",
            "file_path": abs_path,
            "file_name": file_name,
            "size_bytes": len(content),
        }
    except httpx.TimeoutException:
        return {"status": "error", "message": "Download timed out. The attachment may be too large. You may retry once.", "retry": True}
    except Exception as e:
        return {"status": "error", "message": f"Failed to download attachment: {e}. Do not retry — inform the user.", "retry": False}


def _cc_recipients(cc: str | None) -> list[dict]:
    """Build the Graph ``ccRecipients`` list from a CC string.

    Accepts a single address or several comma-separated ones; blanks are
    dropped. Returns ``[]`` when there is no usable address."""
    if not cc:
        return []
    addrs = [a.strip() for a in str(cc).split(",") if a.strip()]
    return [{"emailAddress": {"address": a}} for a in addrs]


class _AttachmentError(Exception):
    """A requested attachment could not be resolved/read or is too large."""


# Graph's simple POST /sendMail caps small attachments (~3-4MB total once
# base64-encoded). Charts (HTML) and CSV reports are well under this.
_MAX_ATTACHMENT_BYTES = 3 * 1024 * 1024


def _read_generated_file(path: str) -> tuple[str, bytes]:
    """Resolve a download path returned by tool_visualize_data /
    create_downloadable_file (e.g. "/api/files/<agent>/<file>") to
    (filename, bytes), reading from local disk or S3 per storage_mode."""
    rel = path.split("/api/files/", 1)[-1].strip("/")
    if "/" not in rel:
        raise _AttachmentError(
            f"Invalid attachment path '{path}'. Pass the exact path returned by "
            "tool_visualize_data / create_downloadable_file — do not invent one."
        )
    filename = os.path.basename(rel)
    from th2agent.configs.settings import get_settings

    if get_settings().storage_mode == "local":
        disk = str(uploads_dir().joinpath(*rel.split("/")))
        if not os.path.isfile(disk):
            raise _AttachmentError(
                f"Attachment not found: '{path}'. Generate the file first."
            )
        with open(disk, "rb") as fh:
            return filename, fh.read()

    from th2agent.storage.s3 import download_file_from_s3, file_exists_in_s3

    key = f"uploads/{rel}"
    if not file_exists_in_s3(key):
        raise _AttachmentError(
            f"Attachment not found: '{path}'. Generate the file first."
        )
    return filename, download_file_from_s3(key)


def _build_graph_attachments(attachments: str) -> list[dict]:
    """Turn a comma-separated list of generated-file paths into Microsoft Graph
    fileAttachment objects (base64). Raises _AttachmentError on a bad/missing
    path or an oversized file."""
    paths = [a.strip() for a in (attachments or "").split(",") if a.strip()]
    out: list[dict] = []
    for p in paths:
        filename, content = _read_generated_file(p)
        if len(content) > _MAX_ATTACHMENT_BYTES:
            raise _AttachmentError(
                f"Attachment '{filename}' is too large to email "
                f"({len(content) // 1024} KB; limit ~3 MB). Send a download link instead."
            )
        out.append({
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": filename,
            "contentBytes": base64.b64encode(content).decode("ascii"),
        })
    return out


def tool_send_outlook_email(
    to: str,
    subject: str,
    body: str,
    cc: str | None = None,
    body_type: str = "Text",
    mailbox: str = "",
    attachments: str = "",
) -> dict:
    """Send an email via the connected Outlook / Microsoft 365 account.

    Uses Microsoft Graph API POST /me/sendMail. The email is sent from the
    account linked via the Microsoft integration and is saved to Sent Items.

    To email a chart or a report, FIRST generate the file with another tool
    (tool_visualize_data for a chart, create_downloadable_file for a CSV/report),
    then pass the EXACT download path it returned (e.g. /api/files/<agent>/<file>)
    as `attachments` here.

    Args:
        to: Recipient email address (single address).
        subject: Subject line of the email.
        body: Body content of the email.
        cc: Optional CC email address (single address).
        body_type: Content type of the body — "Text" (default) or "HTML".
        mailbox: If provided, send from this shared mailbox email address.
        attachments: Optional comma-separated list of file paths to attach, as
            returned by tool_visualize_data / create_downloadable_file
            (e.g. "/api/files/agent1196/colis.html"). Do NOT invent paths.

    Returns:
        dict with keys: status, message.
    """
    try:
        headers = _graph_headers()
    except IntegrationStatusError as e:
        return e.as_tool_result()
    except RuntimeError as e:
        return {"status": "error", "message": str(e), "retry": False}

    try:
        graph_attachments = _build_graph_attachments(attachments)
    except _AttachmentError as e:
        return {"status": "error", "message": str(e), "retry": False}

    message: dict = {
        "subject": subject,
        "body": {
            "contentType": body_type,
            "content": body,
        },
        "toRecipients": [
            {"emailAddress": {"address": to}}
        ],
    }

    _cc = _cc_recipients(cc)
    if _cc:
        message["ccRecipients"] = _cc

    if graph_attachments:
        message["attachments"] = graph_attachments

    payload = {
        "message": message,
        "saveToSentItems": True,
    }

    try:
        resp = httpx.post(
            f"{_mailbox_base(mailbox)}/sendMail",
            headers={**headers, "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        if resp.status_code == 202:
            try:
                import zoneinfo
                _paris = zoneinfo.ZoneInfo("Europe/Paris")
            except Exception:
                import pytz
                _paris = pytz.timezone("Europe/Paris")
            _sent_at = datetime.datetime.now(tz=_paris).strftime("%Y-%m-%d %H:%M")
            logger.info("Email sent to %s (subject: %s)", to, subject)
            return {"status": "success", "message": f"Email sent successfully to {to}.", "sent_at": _sent_at}
        if resp.status_code == 401:
            return {
                "status": "error",
                "message": "Authentication expired. The user needs to reconnect their Outlook account. Do not retry.",
                "retry": False,
            }
        return {
            "status": "error",
            "message": f"Graph API error (HTTP {resp.status_code}): {resp.text[:300]}. Do not retry — inform the user.",
            "retry": False,
        }
    except httpx.TimeoutException:
        return {"status": "error", "message": "Request timed out. You may retry once.", "retry": True}
    except Exception as e:
        return {"status": "error", "message": f"Failed to send email: {e}. Do not retry — inform the user.", "retry": False}
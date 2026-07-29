import os
import re
from logging import getLogger

import httpx

from th2agent.tools_store.portfolio.integration_status import (
    INTEGRATION_BLOCKED_BY_TENANT,
    IntegrationStatusError,
)
from th2agent.tools_store.portfolio.microsoft_auth import microsoft_auth_headers

logger = getLogger(__name__)

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"

_TEAMS_SCOPE = "offline_access Chat.Read Chat.ReadWrite ChatMessage.Send"
_TEAMS_LABEL = "Microsoft Teams"

_integration_loaded: bool = False


def _ensure_integration_tokens() -> None:
    """Lazily load Microsoft Teams integration tokens from DB into env vars."""
    global _integration_loaded
    if _integration_loaded:
        return

    owner = os.getenv("AGENT_OWNER")
    if not owner:
        return

    try:
        from th2agent.integrations.helpers import fetch_integration_configs
        configs = fetch_integration_configs("microsoft_teams")
        refresh_token = configs.get("refresh_token")
        if refresh_token:
            os.environ["TEAMS_REFRESH_TOKEN"] = refresh_token
            logger.info("Microsoft Teams tokens loaded for AGENT_OWNER=%s", owner)
        else:
            logger.warning(
                "Microsoft Teams integration found but refresh_token is empty for AGENT_OWNER=%s",
                owner,
            )
    except Exception as e:
        logger.warning("Could not load Microsoft Teams integration tokens: %s", e)
    finally:
        _integration_loaded = True


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _graph_headers() -> dict[str, str]:
    """Return Authorization header dict for Microsoft Graph calls."""
    _ensure_integration_tokens()
    return microsoft_auth_headers(
        "TEAMS", scope=_TEAMS_SCOPE, service_label=_TEAMS_LABEL,
    )


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text or "")
    return text.strip()


def _format_message(msg: dict) -> dict:
    """Normalise a Graph chatMessage into a clean dict.

    Notes:
    - from.user.displayName is NULL in v1.0 responses — falls back to
      from.user.id so the agent always has an identifier.
    - Content is stripped of HTML tags and capped at 1000 chars.
    """
    body       = msg.get("body") or {}
    content    = body.get("content") or ""
    if body.get("contentType") == "html":
        content = _strip_html(content)

    sender_obj   = (msg.get("from") or {})
    user_obj     = (sender_obj.get("user") or {})
    display_name = user_obj.get("displayName") or user_obj.get("id") or "Unknown"

    return {
        "id":          msg.get("id"),
        "createdAt":   msg.get("createdDateTime"),
        "sender":      display_name,
        "userId":      user_obj.get("id"),
        "content":     content[:1000],
        "messageType": msg.get("messageType", "message"),
        "importance":  msg.get("importance", "normal"),
        "webUrl":      msg.get("webUrl"),
    }


def _handle_graph_error(resp: httpx.Response, context: str) -> dict | None:
    """Return a structured error dict for non-2xx Graph responses, else None."""
    if resp.status_code == 401:
        return {"status": "error", "message": "Authentication expired. The user needs to reconnect Teams. Do not retry.", "retry": False}
    if resp.status_code == 403:
        return {"status": "error", "message": f"Permission denied for {context} (HTTP 403). The Teams integration may be missing required scopes. Do not retry.", "retry": False}
    if resp.status_code == 404:
        return {"status": "error", "message": f"Resource not found for {context}. The ID may be invalid or deleted. Do not retry with the same ID.", "retry": False}
    if resp.status_code == 423:
        # Tenant-level App Access Policy block (SharePoint admin center).
        # Reconnecting will not help — surface a structured status code.
        return IntegrationStatusError(
            code=INTEGRATION_BLOCKED_BY_TENANT,
            provider="microsoft",
            message=(
                f"Microsoft Teams refused {context} with HTTP 423 "
                "(resourceLocked). The user's Microsoft 365 administrator "
                "must allow this app in the SharePoint admin center → "
                "Access control. Reconnecting will not help."
            ),
        ).as_tool_result()
    if resp.status_code >= 400:
        return {"status": "error", "message": f"Graph API error for {context} (HTTP {resp.status_code}): {resp.text[:300]}. Do not retry.", "retry": False}
    return None


# ---------------------------------------------------------------------------
# Public tools
# ---------------------------------------------------------------------------


def tool_list_chats(
    chat_type: str | None = None,
    top: int = 20,
) -> dict:
    """List the current user's Microsoft Teams chats.

    Returns 1:1 conversations, group chats, and meeting chats ordered by most
    recent activity. Does NOT include Teams channel conversations.

    Args:
        chat_type: Optional filter — one of "oneOnOne", "group", "meeting".
            If omitted, all types are returned.
        top: Maximum number of chats to return (1–50). Defaults to 20.

    Returns:
        dict with keys: status, chats (list), total.
        Each chat: id, topic, chatType, lastActivityDateTime,
        lastMessagePreview, webUrl.
    """
    try:
        headers = _graph_headers()
    except IntegrationStatusError as e:
        return e.as_tool_result()
    except RuntimeError as e:
        return {"status": "error", "message": str(e), "retry": False}

    top = max(1, min(top, 50))

    if chat_type:
        valid_types = {"oneOnOne", "group", "meeting"}
        if chat_type not in valid_types:
            return {"status": "error", "message": f"Invalid chat_type '{chat_type}'. Must be one of: {', '.join(sorted(valid_types))}.", "retry": False}

    params: dict[str, str] = {
        "$top":     str(top),
        "$orderby": "lastMessagePreview/createdDateTime desc",
        "$expand":  "lastMessagePreview",
    }
    if chat_type:
        params["$filter"] = f"chatType eq '{chat_type}'"

    try:
        resp = httpx.get(f"{_GRAPH_BASE}/me/chats", headers=headers, params=params, timeout=30)
        err = _handle_graph_error(resp, "list chats")
        if err:
            return err

        chats = []
        for c in resp.json().get("value", []):
            last_preview = c.get("lastMessagePreview") or {}
            preview_body = _strip_html((last_preview.get("body") or {}).get("content") or "")
            chats.append({
                "id":                   c.get("id"),
                "topic":                c.get("topic") or "(no topic)",
                "chatType":             c.get("chatType"),
                "lastActivityDateTime": c.get("lastUpdatedDateTime"),
                "lastMessagePreview":   preview_body[:200],
                "webUrl":               c.get("webUrl"),
            })
        return {"status": "success", "chats": chats, "total": len(chats)}

    except httpx.TimeoutException:
        return {"status": "error", "message": "Request timed out. You may retry once.", "retry": True}
    except Exception as e:
        return {"status": "error", "message": f"Failed to list chats: {e}. Do not retry.", "retry": False}


def tool_find_chat_with_person(person_name: str) -> dict:
    """Find the Teams chat(s) that include a specific person by display name.

    ALWAYS call this tool first when the user asks for messages from a
    conversation with a specific person (e.g. "messages from David Gnaglo").
    This resolves the person's name to a chat_id, which you then pass to
    tool_get_chat_messages.

    For 1:1 chats the topic is usually empty — member lookup is the only
    reliable way to identify them by person name.

    NOTE: $select is intentionally omitted from the members request — some
    tenants return 400 when $select is used on /members. All fields are
    returned and filtered client-side.

    Args:
        person_name: Display name or partial name to search for (case-insensitive).

    Returns:
        dict with keys: status, matches (list), total.
        Each match: chatId, chatType, topic, memberNames, webUrl.
        Pass the chatId of the best match to tool_get_chat_messages.
    """
    try:
        headers = _graph_headers()
    except IntegrationStatusError as e:
        return e.as_tool_result()
    except RuntimeError as e:
        return {"status": "error", "message": str(e), "retry": False}

    needle = person_name.lower().strip()

    try:
        chats_resp = httpx.get(
            f"{_GRAPH_BASE}/me/chats",
            headers=headers,
            params={"$top": "50"},
            timeout=30,
        )
        err = _handle_graph_error(chats_resp, "list chats for person search")
        if err:
            return err
        chats = chats_resp.json().get("value", [])
    except httpx.TimeoutException:
        return {"status": "error", "message": "Request timed out. You may retry once.", "retry": True}
    except Exception as e:
        return {"status": "error", "message": f"Failed to fetch chats: {e}. Do not retry.", "retry": False}

    matches = []

    for chat in chats:
        chat_id = chat.get("id")
        try:
            # Intentionally no $select — some tenants reject it on /members
            mem_resp = httpx.get(
                f"{_GRAPH_BASE}/me/chats/{chat_id}/members",
                headers=headers,
                timeout=15,
            )
            if mem_resp.status_code != 200:
                continue

            members      = mem_resp.json().get("value", [])
            member_names = [m.get("displayName") or "" for m in members]

            if any(needle in name.lower() for name in member_names):
                matches.append({
                    "chatId":      chat_id,
                    "chatType":    chat.get("chatType"),
                    "topic":       chat.get("topic") or "(no topic)",
                    "memberNames": member_names,
                    "webUrl":      chat.get("webUrl"),
                })
        except Exception:
            continue

    if not matches:
        return {
            "status":  "success",
            "matches": [],
            "total":   0,
            "message": (
                f"No chats found with a member matching '{person_name}'. "
                "The person may not have an active Teams chat with you, or the name may be spelled differently."
            ),
        }

    return {"status": "success", "matches": matches, "total": len(matches)}


def tool_get_chat(chat_id: str) -> dict:
    """Get details for a specific Teams chat by its ID.

    Args:
        chat_id: The unique chat ID (from tool_list_chats or tool_find_chat_with_person).

    Returns:
        dict with keys: status, id, topic, chatType, createdDateTime,
        lastUpdatedDateTime, webUrl, memberCount.
    """
    try:
        headers = _graph_headers()
    except IntegrationStatusError as e:
        return e.as_tool_result()
    except RuntimeError as e:
        return {"status": "error", "message": str(e), "retry": False}

    try:
        resp = httpx.get(f"{_GRAPH_BASE}/me/chats/{chat_id}", headers=headers, timeout=30)
        err = _handle_graph_error(resp, f"get chat {chat_id}")
        if err:
            return err

        c = resp.json()

        member_count = None
        # No $select here either — consistent with tool_find_chat_with_person
        mem_resp = httpx.get(
            f"{_GRAPH_BASE}/me/chats/{chat_id}/members",
            headers=headers,
            timeout=15,
        )
        if mem_resp.status_code == 200:
            member_count = len(mem_resp.json().get("value", []))

        return {
            "status":              "success",
            "id":                  c.get("id"),
            "topic":               c.get("topic") or "(no topic)",
            "chatType":            c.get("chatType"),
            "createdDateTime":     c.get("createdDateTime"),
            "lastUpdatedDateTime": c.get("lastUpdatedDateTime"),
            "webUrl":              c.get("webUrl"),
            "memberCount":         member_count,
        }

    except httpx.TimeoutException:
        return {"status": "error", "message": "Request timed out. You may retry once.", "retry": True}
    except Exception as e:
        return {"status": "error", "message": f"Failed to get chat: {e}. Do not retry.", "retry": False}


def tool_get_chat_messages(
    chat_id: str,
    top: int = 20,
    after_date: str | None = None,
    filter_sender_name: str | None = None,
) -> dict:
    """Retrieve messages from a specific Teams chat.

    IMPORTANT: If you have a person's name but not a chat_id, call
    tool_find_chat_with_person first to resolve the chat_id, then call this.

    Graph API constraints:
    - $select is NOT supported on this endpoint (causes 400).
    - Date filtering uses lastModifiedDateTime with 'gt' operator, paired
      with matching $orderby — this is the only supported filter.
    - Sender name filtering is client-side (displayName may be null in
      API responses; userId is always present as fallback).

    Args:
        chat_id: The chat ID (from tool_list_chats or tool_find_chat_with_person).
        top: Maximum number of messages to return (1–50). Defaults to 20.
        after_date: If provided, only return messages after this date.
            Format: "YYYY-MM-DD" (e.g. "2026-03-01").
        filter_sender_name: If provided, only return messages where sender
            display name contains this string (case-insensitive, client-side).

    Returns:
        dict with keys: status, messages (list), total.
        Each message: id, createdAt, sender, userId, content, messageType,
        importance, webUrl.
    """
    try:
        headers = _graph_headers()
    except IntegrationStatusError as e:
        return e.as_tool_result()
    except RuntimeError as e:
        return {"status": "error", "message": str(e), "retry": False}

    top = max(1, min(top, 50))

    params: dict[str, str] = {"$top": str(top)}
    if after_date:
        params["$orderby"] = "lastModifiedDateTime desc"
        params["$filter"]  = f"lastModifiedDateTime gt {after_date}T00:00:00Z"
    else:
        params["$orderby"] = "createdDateTime desc"

    try:
        resp = httpx.get(
            f"{_GRAPH_BASE}/me/chats/{chat_id}/messages",
            headers=headers,
            params=params,
            timeout=30,
        )
        err = _handle_graph_error(resp, f"get messages for chat {chat_id}")
        if err:
            return err

        messages = [_format_message(m) for m in resp.json().get("value", [])]

        if filter_sender_name:
            needle   = filter_sender_name.lower()
            messages = [m for m in messages if needle in (m.get("sender") or "").lower()]

        messages = messages[:top]
        return {"status": "success", "messages": messages, "total": len(messages)}

    except httpx.TimeoutException:
        return {"status": "error", "message": "Request timed out. You may retry once.", "retry": True}
    except Exception as e:
        return {"status": "error", "message": f"Failed to get chat messages: {e}. Do not retry.", "retry": False}


def tool_get_chat_members(chat_id: str) -> dict:
    """List all members of a specific Teams chat.

    Args:
        chat_id: The chat ID (from tool_list_chats or tool_find_chat_with_person).

    Returns:
        dict with keys: status, members (list), total.
        Each member: id, displayName, email, roles.
    """
    try:
        headers = _graph_headers()
    except IntegrationStatusError as e:
        return e.as_tool_result()
    except RuntimeError as e:
        return {"status": "error", "message": str(e), "retry": False}

    try:
        # No $select — consistent with tool_find_chat_with_person to avoid 400s
        resp = httpx.get(
            f"{_GRAPH_BASE}/me/chats/{chat_id}/members",
            headers=headers,
            timeout=30,
        )
        err = _handle_graph_error(resp, f"get members for chat {chat_id}")
        if err:
            return err

        members = [
            {
                "id":          m.get("id"),
                "displayName": m.get("displayName"),
                "email":       m.get("email"),
                "roles":       m.get("roles", []),
            }
            for m in resp.json().get("value", [])
        ]
        return {"status": "success", "members": members, "total": len(members)}

    except httpx.TimeoutException:
        return {"status": "error", "message": "Request timed out. You may retry once.", "retry": True}
    except Exception as e:
        return {"status": "error", "message": f"Failed to get chat members: {e}. Do not retry.", "retry": False}


def tool_search_chat_messages(
    query: str,
    top: int = 10,
    chat_type: str | None = None,
) -> dict:
    """Search for messages containing a keyword across all Teams chats.

    Graph does not expose a cross-chat search endpoint, so this tool fetches
    the user's 30 most recent chats and searches message content locally.

    Args:
        query: Keyword or phrase to search for (case-insensitive).
        top: Maximum number of matching messages to return (1–50). Defaults to 10.
        chat_type: Optionally restrict to "oneOnOne", "group", or "meeting".

    Returns:
        dict with keys: status, results (list), total.
        Each result includes all message fields plus: chatId, chatTopic, chatType.
    """
    try:
        headers = _graph_headers()
    except IntegrationStatusError as e:
        return e.as_tool_result()
    except RuntimeError as e:
        return {"status": "error", "message": str(e), "retry": False}

    top    = max(1, min(top, 50))
    needle = query.lower()

    if chat_type:
        valid_types = {"oneOnOne", "group", "meeting"}
        if chat_type not in valid_types:
            return {"status": "error", "message": f"Invalid chat_type '{chat_type}'. Must be one of: {', '.join(sorted(valid_types))}.", "retry": False}

    chat_params: dict[str, str] = {
        "$top":     "30",
        "$orderby": "lastMessagePreview/createdDateTime desc",
        "$expand":  "lastMessagePreview",
    }
    if chat_type:
        chat_params["$filter"] = f"chatType eq '{chat_type}'"

    try:
        chats_resp = httpx.get(f"{_GRAPH_BASE}/me/chats", headers=headers, params=chat_params, timeout=30)
        err = _handle_graph_error(chats_resp, "list chats for search")
        if err:
            return err
        chats = chats_resp.json().get("value", [])
    except httpx.TimeoutException:
        return {"status": "error", "message": "Request timed out fetching chats. You may retry once.", "retry": True}
    except Exception as e:
        return {"status": "error", "message": f"Failed to fetch chats for search: {e}. Do not retry.", "retry": False}

    results: list[dict] = []

    for chat in chats:
        if len(results) >= top:
            break

        chat_id    = chat.get("id")
        chat_topic = chat.get("topic") or "(no topic)"
        chat_type_ = chat.get("chatType")

        try:
            msgs_resp = httpx.get(
                f"{_GRAPH_BASE}/me/chats/{chat_id}/messages",
                headers=headers,
                params={"$top": "20", "$orderby": "createdDateTime desc"},
                timeout=20,
            )
            if msgs_resp.status_code != 200:
                logger.warning("Skipping chat %s in search — HTTP %s", chat_id, msgs_resp.status_code)
                continue

            for msg in msgs_resp.json().get("value", []):
                raw_content = (msg.get("body") or {}).get("content") or ""
                if needle in _strip_html(raw_content).lower():
                    formatted = _format_message(msg)
                    formatted["chatId"]    = chat_id
                    formatted["chatTopic"] = chat_topic
                    formatted["chatType"]  = chat_type_
                    results.append(formatted)
                    if len(results) >= top:
                        break
        except Exception:
            continue

    return {"status": "success", "results": results, "total": len(results)}


def tool_get_my_recent_mentions(
    top: int = 10,
    after_date: str | None = None,
) -> dict:
    """Find recent Teams messages where the current user was @mentioned.

    Resolves the current user's ID and display name via /me, then scans the
    30 most recent chats for messages that contain a direct @mention of the
    user OR an @everyone / @channel broadcast mention.

    Checks (in order of reliability):
    1. Structured ``mentions`` array — user match by id, displayName, or UPN.
    2. Structured ``mentions`` array — conversation/channel-level mention
       (i.e. @everyone, @team, @channel).
    3. Raw HTML body fallback — ``<at>`` tags containing the user's name,
       "everyone", "channel", or "team" (catches older messages and tenants
       that don't populate the mentions array).

    Args:
        top: Maximum number of matching messages to return (1–50). Defaults to 10.
        after_date: If provided, only return messages on or after this date.
            Format: "YYYY-MM-DD" (e.g. "2026-03-10"). When the caller says
            "today", pass today's date here. Filtering is client-side because
            Graph does not support $filter on /messages with createdDateTime.

    Returns:
        dict with keys: status, mentions (list), total.
        Each mention includes all message fields plus: chatId, chatTopic, chatType.
    """
    try:
        headers = _graph_headers()
    except IntegrationStatusError as e:
        return e.as_tool_result()
    except RuntimeError as e:
        return {"status": "error", "message": str(e), "retry": False}

    top = max(1, min(top, 50))

    # Parse after_date once so we can compare against createdDateTime strings
    after_date_prefix: str | None = None
    if after_date:
        # createdDateTime looks like "2026-03-10T09:10:47.987Z" — prefix compare works
        after_date_prefix = after_date.strip()  # "YYYY-MM-DD"

    # -- 1. Resolve current user (id + displayName + UPN) -------------------
    try:
        me_resp = httpx.get(
            f"{_GRAPH_BASE}/me",
            headers=headers,
            params={"$select": "id,displayName,userPrincipalName"},
            timeout=15,
        )
        err = _handle_graph_error(me_resp, "get /me profile")
        if err:
            return err
        me_data = me_resp.json()
        my_id   = me_data.get("id") or ""
        my_name = (me_data.get("displayName") or "").lower()
        my_upn  = (me_data.get("userPrincipalName") or "").lower()
        if not my_id and not my_name:
            return {"status": "error", "message": "Could not determine the current user's identity. Do not retry.", "retry": False}
    except Exception as e:
        return {"status": "error", "message": f"Failed to fetch user profile: {e}. Do not retry.", "retry": False}

    # -- 2. Fetch recent chats -----------------------------------------------
    try:
        chats_resp = httpx.get(
            f"{_GRAPH_BASE}/me/chats",
            headers=headers,
            params={
                "$top":     "30",
                "$orderby": "lastMessagePreview/createdDateTime desc",
                "$expand":  "lastMessagePreview",
            },
            timeout=30,
        )
        err = _handle_graph_error(chats_resp, "list chats for mentions")
        if err:
            return err
        chats = chats_resp.json().get("value", [])
    except httpx.TimeoutException:
        return {"status": "error", "message": "Request timed out. You may retry once.", "retry": True}
    except Exception as e:
        return {"status": "error", "message": f"Failed to fetch chats: {e}. Do not retry.", "retry": False}

    mentions: list[dict] = []

    # Keywords that indicate a broadcast / @everyone mention in raw HTML bodies
    _BROADCAST_KEYWORDS = ("everyone", "channel", "team", "général", "general")

    def _is_mention_of_me(msg: dict) -> bool:
        """True if this message @mentions the current user or @everyone/channel."""
        # -- Primary: structured mentions array --
        for m in (msg.get("mentions") or []):
            mentioned = (m.get("mentioned") or {})

            # Direct user mention
            user = (mentioned.get("user") or {})
            if user:
                if my_id and user.get("id") == my_id:
                    return True
                dn = (user.get("displayName") or "").lower()
                if my_name and my_name in dn:
                    return True
                upn = (user.get("userPrincipalName") or "").lower()
                if my_upn and my_upn in upn:
                    return True

            # Conversation/channel-level mention (@everyone, @team, @channel)
            conversation = (mentioned.get("conversation") or {})
            if conversation:
                # Any conversation-level mention in a chat the user is part of
                # counts as an @everyone equivalent
                return True

        # -- Fallback: raw HTML body --
        raw_body = (msg.get("body") or {}).get("content") or ""
        raw_lower = raw_body.lower()

        # User name in body (plain-text or inside <at> tag)
        if my_name and my_name in raw_lower:
            return True

        # @everyone / @channel patterns inside <at>...</at> tags
        at_tags = re.findall(r"<at[^>]*>(.*?)</at>", raw_body, re.IGNORECASE)
        for tag_text in at_tags:
            if any(kw in tag_text.lower() for kw in _BROADCAST_KEYWORDS):
                return True

        return False

    for chat in chats:
        if len(mentions) >= top:
            break
        chat_id    = chat.get("id")
        chat_topic = chat.get("topic") or "(no topic)"
        chat_type_ = chat.get("chatType")
        try:
            # Fetch 50 messages so busy chats aren't truncated
            msgs_resp = httpx.get(
                f"{_GRAPH_BASE}/me/chats/{chat_id}/messages",
                headers=headers,
                params={"$top": "50", "$orderby": "createdDateTime desc"},
                timeout=20,
            )
            if msgs_resp.status_code != 200:
                continue
            for msg in msgs_resp.json().get("value", []):
                if msg.get("messageType") != "message":
                    continue

                # Client-side date filter (Graph doesn't support createdDateTime $filter here)
                if after_date_prefix:
                    created = msg.get("createdDateTime") or ""
                    if created[:10] < after_date_prefix:
                        # Messages are newest-first; once we go past the date, stop
                        # but don't break — other messages in the same batch could
                        # still be on the right date if ordering is imperfect
                        continue

                if _is_mention_of_me(msg):
                    formatted = _format_message(msg)
                    formatted["chatId"]    = chat_id
                    formatted["chatTopic"] = chat_topic
                    formatted["chatType"]  = chat_type_
                    mentions.append(formatted)
                    if len(mentions) >= top:
                        break
        except Exception:
            continue

    return {"status": "success", "mentions": mentions, "total": len(mentions)}


def tool_list_chat_attachments(chat_id: str, top: int = 20) -> dict:
    """List file attachments shared in a Teams chat.

    Scans recent messages in the chat and collects all file/card attachments.
    Returns metadata and download URLs — the URLs are direct OneDrive/SharePoint
    links that can be opened in a browser.

    Args:
        chat_id: The chat ID (from tool_list_chats or tool_find_chat_with_person).
        top: Maximum number of attachments to return (1–50). Defaults to 20.

    Returns:
        dict with keys: status, attachments (list), total, note.
        Each attachment: messageId, sentAt, sender, name, contentType,
        contentUrl, thumbnailUrl.
    """
    try:
        headers = _graph_headers()
    except IntegrationStatusError as e:
        return e.as_tool_result()
    except RuntimeError as e:
        return {"status": "error", "message": str(e), "retry": False}

    top = max(1, min(top, 50))

    try:
        # Fetch up to 50 recent messages to collect attachments from
        msgs_resp = httpx.get(
            f"{_GRAPH_BASE}/me/chats/{chat_id}/messages",
            headers=headers,
            params={"$top": "50", "$orderby": "createdDateTime desc"},
            timeout=30,
        )
        err = _handle_graph_error(msgs_resp, "list chat messages for attachments")
        if err:
            return err
        messages = msgs_resp.json().get("value", [])
    except httpx.TimeoutException:
        return {"status": "error", "message": "Request timed out. You may retry once.", "retry": True}
    except Exception as e:
        return {"status": "error", "message": f"Failed to fetch messages: {e}. Do not retry.", "retry": False}

    attachments: list[dict] = []

    for msg in messages:
        if len(attachments) >= top:
            break
        if msg.get("messageType") != "message":
            continue
        msg_attachments = msg.get("attachments") or []
        if not msg_attachments:
            continue

        sender_obj  = (msg.get("from") or {})
        user_obj    = (sender_obj.get("user") or {})
        sender_name = user_obj.get("displayName") or user_obj.get("id") or "Unknown"
        sent_at     = msg.get("createdDateTime")
        msg_id      = msg.get("id")

        for att in msg_attachments:
            content_type = att.get("contentType") or ""

            # Skip message quotes/replies and Teams UI cards — these are not
            # downloadable files. Only "reference" (OneDrive/SharePoint) and
            # "file" (direct upload) are real file attachments.
            _SKIP_TYPES = {
                "messagereference",
                "application/vnd.microsoft.card.adaptive",
                "application/vnd.microsoft.card.hero",
                "application/vnd.microsoft.card.thumbnail",
                "application/vnd.microsoft.card.signin",
                "application/vnd.microsoft.card.receipt",
                "application/vnd.microsoft.card.list",
                "application/vnd.microsoft.card.o365connector",
            }
            if content_type.lower() in _SKIP_TYPES:
                continue

            # Skip entries with no usable download URL
            content_url = att.get("contentUrl") or ""
            if not content_url:
                continue

            name      = att.get("name") or att.get("id") or "unnamed"
            thumbnail = att.get("thumbnailUrl") or ""

            attachments.append({
                "messageId":    msg_id,
                "sentAt":       sent_at,
                "sender":       sender_name,
                "name":         name,
                "contentType":  content_type,
                "contentUrl":   content_url,
                "thumbnailUrl": thumbnail,
            })

            if len(attachments) >= top:
                break

    return {
        "status":      "success",
        "attachments": attachments,
        "total":       len(attachments),
        "note":        "Open 'contentUrl' in a browser to download the file. URLs may require authentication.",
    }



def tool_send_message(
    chat_id: str,
    content: str,
    importance: str = "normal",
) -> dict:
    """Send a plain message to an existing Teams chat.

    Use tool_find_chat_with_person to resolve a person's name to a chat_id first.
    Use tool_create_group_chat to start a brand-new group conversation.
    Use tool_reply_to_message if you want to reply to a specific message with
    its context quoted.

    Args:
        chat_id:    The chat ID to send the message to.
        content:    Plain-text message content. Max 28,000 characters.
        importance: "normal" (default) | "high" | "urgent".
                    "urgent" sends repeated push notifications every 2 minutes
                    for 20 minutes — use only for genuine urgency.

    Returns:
        dict with keys: status, id, createdAt, webUrl.
    """
    try:
        headers = _graph_headers()
    except IntegrationStatusError as e:
        return e.as_tool_result()
    except RuntimeError as e:
        return {"status": "error", "message": str(e), "retry": False}

    if not chat_id or not chat_id.strip():
        return {"status": "error", "message": "chat_id must not be empty.", "retry": False}

    if not content or not content.strip():
        return {"status": "error", "message": "content must not be empty.", "retry": False}

    if importance not in {"normal", "high", "urgent"}:
        importance = "normal"

    try:
        resp = httpx.post(
            f"{_GRAPH_BASE}/me/chats/{chat_id}/messages",
            headers={**headers, "Content-Type": "application/json"},
            json={
                "body": {
                    "contentType": "text",
                    "content":     content[:28000],
                },
                "importance": importance,
            },
            timeout=30,
        )
        err = _handle_graph_error(resp, f"send message to chat {chat_id}")
        if err:
            return err

        msg = resp.json()
        return {
            "status":    "success",
            "id":        msg.get("id"),
            "createdAt": msg.get("createdDateTime"),
            "webUrl":    msg.get("webUrl"),
            "message":   "Message sent successfully.",
        }

    except httpx.TimeoutException:
        return {"status": "error", "message": "Request timed out. You may retry once.", "retry": True}
    except Exception as e:
        return {"status": "error", "message": f"Failed to send message: {e}. Do not retry.", "retry": False}


def tool_reply_to_message(
    chat_id: str,
    message_id: str,
    reply_content: str,
    importance: str = "normal",
) -> dict:
    """Fetch a specific message, understand its context, and send a quoted reply.

    This tool:
    1. Fetches the original message so you can read its full content and sender.
    2. Builds a quote-reply block with the original message visible above your reply.
    3. Sends the combined HTML to Teams so recipients see the context.

    WHEN TO USE: When the user says "reply to [person]'s message", "respond to
    that message", or similar — use this instead of tool_send_message so the
    reply is anchored to the original.

    WORKFLOW:
    - Call tool_get_chat_messages first to find the message_id you want to reply to.
    - Then call this tool with your composed reply_content.

    Args:
        chat_id:       The chat ID containing the message.
        message_id:    The ID of the message to reply to (from tool_get_chat_messages).
        reply_content: Your reply text (plain text — this tool handles HTML wrapping).
                       Max 28,000 characters.
        importance:    "normal" (default) | "high" | "urgent".

    Returns:
        dict with keys: status, id, createdAt, webUrl, quoted_sender, quoted_preview.
    """
    try:
        headers = _graph_headers()
    except IntegrationStatusError as e:
        return e.as_tool_result()
    except RuntimeError as e:
        return {"status": "error", "message": str(e), "retry": False}

    if not chat_id or not chat_id.strip():
        return {"status": "error", "message": "chat_id must not be empty.", "retry": False}
    if not message_id or not message_id.strip():
        return {"status": "error", "message": "message_id must not be empty.", "retry": False}
    if not reply_content or not reply_content.strip():
        return {"status": "error", "message": "reply_content must not be empty.", "retry": False}

    if importance not in {"normal", "high", "urgent"}:
        importance = "normal"

    # -- Step 1: Fetch the original message ----------------------------------
    try:
        orig_resp = httpx.get(
            f"{_GRAPH_BASE}/me/chats/{chat_id}/messages/{message_id}",
            headers=headers,
            timeout=15,
        )
        err = _handle_graph_error(orig_resp, f"fetch message {message_id}")
        if err:
            return err
        orig = orig_resp.json()
    except httpx.TimeoutException:
        return {"status": "error", "message": "Timed out fetching the original message. You may retry once.", "retry": True}
    except Exception as e:
        return {"status": "error", "message": f"Failed to fetch original message: {e}. Do not retry.", "retry": False}

    # -- Step 2: Extract sender and content for the quote block --------------
    sender_obj    = (orig.get("from") or {})
    user_obj      = (sender_obj.get("user") or {}
    )
    quoted_sender = user_obj.get("displayName") or user_obj.get("id") or "Unknown"
    created_at    = orig.get("createdDateTime") or ""

    orig_body         = (orig.get("body") or {})
    orig_content_type = orig_body.get("contentType") or "text"
    orig_content      = orig_body.get("content") or ""

    # Strip HTML if the original is HTML so the quote preview is readable
    if orig_content_type == "html":
        quoted_preview = _strip_html(orig_content)[:300]
    else:
        quoted_preview = orig_content[:300]

    # -- Step 3: Build a Teams-compatible quote-reply HTML body --------------
    # Teams renders a styled blockquote when the message body is HTML and
    # contains the <attachment> + <blockquote> pattern used by the client.
    # We fall back to a plain-text blockquote style that renders cleanly.
    escaped_preview  = quoted_preview.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    escaped_reply    = reply_content[:28000].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    html_body = (
        f'<blockquote>{escaped_preview}{"…" if len(quoted_preview) == 300 else ""}</blockquote>'
        f'<p>{escaped_reply}</p>'
    )

    # -- Step 4: Send the reply ----------------------------------------------
    try:
        resp = httpx.post(
            f"{_GRAPH_BASE}/me/chats/{chat_id}/messages",
            headers={**headers, "Content-Type": "application/json"},
            json={
                "body": {
                    "contentType": "html",
                    "content":     html_body,
                },
                "importance": importance,
            },
            timeout=30,
        )
        err = _handle_graph_error(resp, f"send reply to message {message_id} in chat {chat_id}")
        if err:
            return err

        msg = resp.json()
        return {
            "status":         "success",
            "id":             msg.get("id"),
            "createdAt":      msg.get("createdDateTime"),
            "webUrl":         msg.get("webUrl"),
            "quoted_sender":  quoted_sender,
            "quoted_preview": quoted_preview,
            "message":        f"Reply to {quoted_sender}'s message sent successfully.",
        }

    except httpx.TimeoutException:
        return {"status": "error", "message": "Request timed out sending reply. You may retry once.", "retry": True}
    except Exception as e:
        return {"status": "error", "message": f"Failed to send reply: {e}. Do not retry.", "retry": False}


def tool_create_group_chat(
    member_emails: list[str],
    topic: str,
) -> dict:
    """Create a new Teams group chat with multiple people.

    Use this to start a fresh group conversation. For 1:1 conversations,
    use tool_find_chat_with_person to check if a chat already exists — if not,
    pass a single email here (topic will be ignored by Teams for 1:1 chats).

    After creating the chat, use tool_send_message with the returned chat_id
    to send the first message.

    NOTE: The current user is automatically added as owner — do NOT include
    their own email in member_emails.

    Args:
        member_emails: List of email addresses (userPrincipalName) to add.
                       Minimum 2 for a proper group chat.
        topic:         Display name shown at the top of the chat.
                       Required and strongly recommended for group chats.

    Returns:
        dict with keys: status, chat_id, chatType, webUrl.
        Pass chat_id to tool_send_message to post the first message.
    """
    try:
        headers = _graph_headers()
    except IntegrationStatusError as e:
        return e.as_tool_result()
    except RuntimeError as e:
        return {"status": "error", "message": str(e), "retry": False}

    if not member_emails or len(member_emails) < 1:
        return {"status": "error", "message": "member_emails must contain at least one email address.", "retry": False}

    if not topic or not topic.strip():
        return {"status": "error", "message": "topic is required for group chats.", "retry": False}

    # Resolve each email to a user ID via Graph
    members: list[dict] = []
    for email in member_emails:
        email = email.strip()
        if not email:
            continue
        try:
            user_resp = httpx.get(
                f"{_GRAPH_BASE}/users/{email}",
                headers=headers,
                params={"$select": "id,displayName,userPrincipalName"},
                timeout=15,
            )
            if user_resp.status_code == 404:
                return {
                    "status":  "error",
                    "message": f"User '{email}' not found in the directory. Verify the email address.",
                    "retry":   False,
                }
            err = _handle_graph_error(user_resp, f"look up user {email}")
            if err:
                return err
            user_id = user_resp.json().get("id")
            if not user_id:
                return {"status": "error", "message": f"Could not resolve a user ID for '{email}'.", "retry": False}
            members.append({
                "@odata.type":     "#microsoft.graph.aadUserConversationMember",
                "roles":           [],
                "user@odata.bind": f"https://graph.microsoft.com/v1.0/users('{user_id}')",
            })
        except httpx.TimeoutException:
            return {"status": "error", "message": f"Timed out looking up '{email}'. You may retry once.", "retry": True}
        except Exception as e:
            return {"status": "error", "message": f"Failed to look up '{email}': {e}. Do not retry.", "retry": False}

    # Add current user as owner
    members.append({
        "@odata.type":     "#microsoft.graph.aadUserConversationMember",
        "roles":           ["owner"],
        "user@odata.bind": "https://graph.microsoft.com/v1.0/me",
    })

    chat_type = "oneOnOne" if len(member_emails) == 1 else "group"

    payload: dict = {
        "chatType": chat_type,
        "members":  members,
    }
    if topic and chat_type == "group":
        payload["topic"] = topic.strip()

    try:
        resp = httpx.post(
            f"{_GRAPH_BASE}/chats",
            headers={**headers, "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        err = _handle_graph_error(resp, "create group chat")
        if err:
            return err

        chat = resp.json()
        chat_id = chat.get("id")
        return {
            "status":   "success",
            "chat_id":  chat_id,
            "chatType": chat.get("chatType"),
            "webUrl":   chat.get("webUrl"),
            "message":  f"Group chat '{topic}' created. Use tool_send_message with chat_id='{chat_id}' to post the first message.",
        }

    except httpx.TimeoutException:
        return {"status": "error", "message": "Request timed out. You may retry once.", "retry": True}
    except Exception as e:
        return {"status": "error", "message": f"Failed to create group chat: {e}. Do not retry.", "retry": False}
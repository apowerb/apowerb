"""Google Calendar tools -- Manage events via Google Calendar API.

Provides 3 tools for listing, creating, and searching calendar events
in a connected Google Calendar account.

Auth credentials are injected as environment variables by the tool_config
system at agent runtime:
  - ``GOOGLE_CALENDAR_REFRESH_TOKEN`` -- OAuth2 refresh token

The shared helper ``google_auth_headers()`` transparently exchanges the
refresh token for a short-lived access token.
"""

from datetime import datetime, timezone
from logging import getLogger

import httpx

from apowerb.tools_store.portfolio.google_auth import google_auth_headers
from apowerb.tools_store.portfolio.integration_status import IntegrationStatusError

logger = getLogger(__name__)

_BASE = "https://www.googleapis.com/calendar/v3"
_SERVICE = "GOOGLE_CALENDAR"


def _format_event(ev: dict) -> dict:
    return {
        "id": ev.get("id"),
        "summary": ev.get("summary"),
        "start": ev.get("start"),
        "end": ev.get("end"),
        "location": ev.get("location"),
        "description": ev.get("description"),
        "htmlLink": ev.get("htmlLink"),
    }


def tool_list_events(
    max_results: int = 10,
    time_min: str | None = None,
    time_max: str | None = None,
    calendar_id: str = "primary",
) -> dict:
    """List upcoming events from a Google Calendar.

    Args:
        max_results: Maximum number of events to return (default 10).
        time_min: Start of time range in ISO 8601 format (e.g. '2026-03-01T00:00:00Z').
            Defaults to now.
        time_max: End of time range in ISO 8601 format. Optional.
        calendar_id: Calendar identifier (default 'primary').

    Returns:
        A dict with status, events list, and total count.
    """
    try:
        headers = google_auth_headers(_SERVICE)
        if not time_min:
            time_min = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        params: dict = {
            "maxResults": max_results,
            "singleEvents": "true",
            "orderBy": "startTime",
            "timeMin": time_min,
        }
        if time_max:
            params["timeMax"] = time_max

        resp = httpx.get(
            f"{_BASE}/calendars/{calendar_id}/events",
            headers=headers,
            params=params,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        events = [_format_event(e) for e in data.get("items", [])]
        return {"status": "ok", "events": events, "total": len(events)}
    except IntegrationStatusError as e:
        return e.as_tool_result()
    except Exception as exc:
        logger.exception("tool_list_events failed")
        return {"status": "error", "message": str(exc), "retry": False}


def tool_create_event(
    summary: str,
    start: str,
    end: str,
    description: str | None = None,
    location: str | None = None,
    attendees: str | None = None,
    calendar_id: str = "primary",
) -> dict:
    """Create a new event in Google Calendar.

    Args:
        summary: Event title.
        start: Start datetime in ISO 8601 format (e.g. '2026-03-10T09:00:00Z').
        end: End datetime in ISO 8601 format.
        description: Optional event description.
        location: Optional event location.
        attendees: Optional comma-separated email addresses of attendees.
        calendar_id: Calendar identifier (default 'primary').

    Returns:
        A dict with status, event_id, and htmlLink.
    """
    try:
        headers = google_auth_headers(_SERVICE)
        body: dict = {
            "summary": summary,
            "start": {"dateTime": start, "timeZone": "UTC"},
            "end": {"dateTime": end, "timeZone": "UTC"},
        }
        if description:
            body["description"] = description
        if location:
            body["location"] = location
        if attendees:
            body["attendees"] = [
                {"email": a.strip()} for a in attendees.split(",") if a.strip()
            ]

        resp = httpx.post(
            f"{_BASE}/calendars/{calendar_id}/events",
            headers=headers,
            json=body,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "status": "ok",
            "event_id": data.get("id"),
            "htmlLink": data.get("htmlLink"),
        }
    except IntegrationStatusError as e:
        return e.as_tool_result()
    except Exception as exc:
        logger.exception("tool_create_event failed")
        return {"status": "error", "message": str(exc), "retry": False}


def tool_search_events(
    query: str,
    max_results: int = 10,
    calendar_id: str = "primary",
) -> dict:
    """Search calendar events by text query.

    Args:
        query: Free-text search term (matches summary, description, location, etc.).
        max_results: Maximum number of events to return (default 10).
        calendar_id: Calendar identifier (default 'primary').

    Returns:
        A dict with status, matching events list, and total count.
    """
    try:
        headers = google_auth_headers(_SERVICE)
        time_min = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        resp = httpx.get(
            f"{_BASE}/calendars/{calendar_id}/events",
            headers=headers,
            params={
                "q": query,
                "maxResults": max_results,
                "singleEvents": "true",
                "orderBy": "startTime",
                "timeMin": time_min,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        events = [_format_event(e) for e in data.get("items", [])]
        return {"status": "ok", "events": events, "total": len(events)}
    except IntegrationStatusError as e:
        return e.as_tool_result()
    except Exception as exc:
        logger.exception("tool_search_events failed")
        return {"status": "error", "message": str(exc), "retry": False}

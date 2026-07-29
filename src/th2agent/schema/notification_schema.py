"""Pydantic schemas for the notifications system.

Covers list, count, and individual notification responses.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class NotificationResponse(BaseModel):
    """A single notification as returned by the API."""

    id: int
    title: str
    message: str | None = None
    type: str
    link: str | None = None
    metadata: dict | None = None  # Parsed from metadata_json
    is_read: bool
    created_at: str | None = None

    model_config = ConfigDict(from_attributes=True)


class NotificationListResponse(BaseModel):
    """Paginated list of notifications."""

    notifications: list[NotificationResponse]


class UnreadCountResponse(BaseModel):
    """Badge count for unread notifications."""

    count: int

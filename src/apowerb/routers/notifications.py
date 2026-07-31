"""Notifications router -- CRUD for user notifications.

Endpoints:
    GET   /api/notifications              (auth)  List notifications (most recent first)
    GET   /api/notifications/unread-count  (auth)  Count of unread notifications
    GET   /api/notifications/stream        (auth)  SSE stream for real-time push
    PATCH /api/notifications/{id}/read     (auth)  Mark a single notification as read
    POST  /api/notifications/read-all      (auth)  Mark all notifications as read
"""

import asyncio
import json
from logging import getLogger

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from apowerb.auth.dependencies import get_current_user
from apowerb.helpers.database import get_db
from apowerb.helpers.notification_bus import subscribe, unsubscribe
from apowerb.models import Notification
from apowerb.schema.notification_schema import (
    NotificationListResponse,
    NotificationResponse,
    UnreadCountResponse,
)
from apowerb.users import schemas as user_schemas

logger = getLogger(__name__)

router = APIRouter(prefix="/notifications", tags=["notifications"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _notification_to_response(notif: Notification) -> NotificationResponse:
    """Convert an ORM Notification to the API response schema."""
    metadata = None
    if notif.metadata_json:
        try:
            metadata = json.loads(notif.metadata_json)
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                "[NOTIFICATIONS] Failed to parse metadata_json for notification %s",
                notif.id,
            )

    return NotificationResponse(
        id=notif.id,
        title=notif.title,
        message=notif.message,
        type=notif.type,
        link=notif.link,
        metadata=metadata,
        is_read=notif.is_read,
        created_at=notif.created_at.isoformat() if notif.created_at else None,
    )


# ---------------------------------------------------------------------------
# 1. GET /api/notifications -- List notifications (authenticated)
# ---------------------------------------------------------------------------


@router.get("")
async def list_notifications(
    db: AsyncSession = Depends(get_db),
    current_user: user_schemas.User = Depends(get_current_user),
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0),
    unread_only: bool = Query(default=False),
) -> NotificationListResponse:
    """Return the current user's notifications, most recent first.

    Supports pagination via ``limit`` / ``offset`` and an optional
    ``unread_only`` filter for showing only unread notifications.
    """
    query = select(Notification).where(
        Notification.user_id == current_user.user_id,
    )
    if unread_only:
        query = query.where(Notification.is_read == False)  # noqa: E712
    query = query.order_by(Notification.created_at.desc()).limit(limit).offset(offset)

    result = await db.execute(query)
    notifications = result.scalars().all()

    return NotificationListResponse(
        notifications=[_notification_to_response(n) for n in notifications]
    )


# ---------------------------------------------------------------------------
# 1b. GET /api/notifications/stream -- SSE real-time push (authenticated)
# ---------------------------------------------------------------------------


@router.get("/stream")
async def notification_stream(
    request: Request,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Server-Sent Events stream that pushes notifications in real time."""
    user_id = current_user.user_id
    queue = subscribe(user_id)

    async def event_generator():
        try:
            while True:
                # Check if client disconnected
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=30)
                    yield f"data: {json.dumps(payload)}\n\n"
                except asyncio.TimeoutError:
                    # Send keepalive to prevent proxy/browser timeout
                    yield ": keepalive\n\n"
        finally:
            unsubscribe(user_id, queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# 2. GET /api/notifications/unread-count -- Unread count (authenticated)
# ---------------------------------------------------------------------------


@router.get("/unread-count")
async def unread_count(
    db: AsyncSession = Depends(get_db),
    current_user: user_schemas.User = Depends(get_current_user),
) -> UnreadCountResponse:
    """Return the number of unread notifications for badge display."""
    result = await db.execute(
        select(func.count(Notification.id)).where(
            Notification.user_id == current_user.user_id,
            Notification.is_read == False,  # noqa: E712
        )
    )
    count = result.scalar_one()

    return UnreadCountResponse(count=count)


# ---------------------------------------------------------------------------
# 3. PATCH /api/notifications/{id}/read -- Mark single as read (authenticated)
# ---------------------------------------------------------------------------


@router.patch("/{notification_id}/read")
async def mark_as_read(
    notification_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: user_schemas.User = Depends(get_current_user),
) -> NotificationResponse:
    """Mark a single notification as read."""
    result = await db.execute(
        select(Notification).where(
            Notification.id == notification_id,
            Notification.user_id == current_user.user_id,
        )
    )
    notification = result.scalar_one_or_none()

    if not notification:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Notification {notification_id} not found.",
        )

    notification.is_read = True  # type: ignore[assignment]
    await db.commit()
    await db.refresh(notification)

    return _notification_to_response(notification)


# ---------------------------------------------------------------------------
# 4. POST /api/notifications/read-all -- Mark all as read (authenticated)
# ---------------------------------------------------------------------------


@router.post("/read-all")
async def mark_all_as_read(
    db: AsyncSession = Depends(get_db),
    current_user: user_schemas.User = Depends(get_current_user),
) -> dict:
    """Mark all of the current user's notifications as read."""
    result = await db.execute(
        update(Notification)
        .where(
            Notification.user_id == current_user.user_id,
            Notification.is_read == False,  # noqa: E712
        )
        .values(is_read=True)
    )
    await db.commit()

    updated_count = result.rowcount  # type: ignore[union-attr]
    logger.info(
        "[NOTIFICATIONS] Marked %d notifications as read for user_id=%s",
        updated_count,
        current_user.user_id,
    )

    return {"success": True, "updated_count": updated_count}
